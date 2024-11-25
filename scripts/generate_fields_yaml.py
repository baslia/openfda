#!/usr/bin/env python

""" Tool for making field values for the documentation website (open.fda.gov)
    that are stored in the appropriate _fields.yaml file. These fields are
    determined by looking both the json schema file and the elasticsearch
    mapping file.

    This tool will only build the skeleton structure for the _fields.yaml, the
    content must be generated by another process, which will most likely be a
    manual one.

    Part of difficulty in generating this yaml from an Elasticsearch schema is
    the non-explicit use of arrays in the Elasticsearch mapping files. JSON has
    an array type and encodes its contents in an `items` key. As a novel of
    apporach for dealing with this impedence mismatch, the code below flattens
    both the Elasticsearch mapping and the JSON schema and then maps the
    schema keys to their Elasticsearch mapping counterparts by removing the
    `.items` key string. It can then use the flatten key name to update the
    schema to have the additional fields needs by the documentation site.
"""

import collections
import logging
import sys

import click
import requests
import simplejson as json
import yaml
from yaml import SafeDumper


@click.group()
def cli():
    pass


# Each field in the schema will have the following data
DEFAULTS = {
    "format": None,
    "type": "string",
    "description": None,
    "possible_values": None,
    "is_exact": False,
}


def completely_flatten(input_data, absolute_key="", sep="."):
    """A Flattening function that takes a highly nested dictionary and flattens
    it into a single depth dictionary with breadcrumb style keys, {a.b.c: val}

    Please note, this will not work with dictionaries that have lists in them.
    """
    result = []
    for key, value in input_data.items():
        appended_key = sep.join([absolute_key, key]) if absolute_key else key

        if isinstance(value, dict):
            result.extend(
                list(completely_flatten(value, appended_key, sep=sep).items())
            )

        else:
            result.append((appended_key, value))

    return dict(result)


def set_deep(data, path, value):
    """Helper function that sets a value deep into a dictionary by passing the
    deep address as a '.' delimited string of keys, for instance, `a.b.c` is
    pointing to {'a': {'b': {'c': 'some value' }}}

    Please note, this is not a pure function, it mutates the object (`data`)
    that is passed into it. Also, this does not work if there are any lists
    within the object.
    """
    path = path.split(".")
    latest = path.pop()
    for key in path:
        data = data.setdefault(key, {})
    data.setdefault(latest, value)


def get_deep(data, path):
    """Helper function that gets a value form deep within an object by passing
    a '.' delimited string of keys, for instance, `a.b.c` is
    pointing to {'a': {'b': {'c': 'some value' }}}.
    """
    path = path.split(".")
    first, rest = path.pop(0), path
    result = data.get(first, {})
    for part in rest:
        result = result.get(part, {})

    return result


def get_exact_fields(mapping):
    """Takes a elasticsearch mapping file, flattens it, filters out exact fields
    and returns a unique list of keys that can be consumed by get_deep()
    """
    result = {}
    exact_lists = "_exact"
    exact_fields = ".fields"
    flat_map = completely_flatten(mapping)
    flat_map = [
        key
        for key, value in flat_map.items()
        if exact_lists in key or exact_fields in key
    ]
    for key in flat_map:
        # Only interested in everything before the _exact or .fields
        part = key.split(exact_lists if exact_lists in key else exact_fields).pop(0)
        # And after the Elasticsearch type name for the mapping (which is the root)
        part = ".".join(part.split(".")[1:])
        result[part] = True

    return result


def _prefix(key):
    """Helper function that removes the last entry from a '.' delimited string."""
    return ".".join(key.split(".")[:-1])


def get_schema_fields(schema):
    result = {}
    for field, value in completely_flatten(schema).items():
        if value == "object":
            continue

        field = _prefix(field)
        result[field] = True

    return list(result.keys())


def get_mapping_fields(mapping):
    result = {}
    for field, value in completely_flatten(mapping).items():
        # And after the Elasticsearch type name for the mapping (which is the root)
        # We also do not need the last key (type)
        part = ".".join(field.split(".")[1:-1])
        result[part] = True

    return result


def generate_es_to_schema_index(schema):
    """Elasticsearch does not natively support the array type (expressed as
    items in JSON Schema), so we need an index with the `items` keys missing
    in order to lookup from Elasticsearch mappings to JSON Schema.
    """
    flattened_schema = completely_flatten(schema)
    result = {}
    for key in flattened_schema.keys():
        es_key = key.replace(".items", "")
        short_key = _prefix(key)
        result[short_key] = _prefix(es_key)
    return result


# YAML hackery to preserve item ordering and pretty string formatting
def tweak_yaml():
    _mapping_tag = yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG

    def dict_constructor(loader, node):
        return collections.OrderedDict(loader.construct_pairs(node))

    yaml.add_constructor(_mapping_tag, dict_constructor)

    def ordered_dict_serializer(self, data):
        return self.represent_mapping("tag:yaml.org,2002:map", list(data.items()))

    SafeDumper.add_representer(collections.OrderedDict, ordered_dict_serializer)
    # We want blanks for null values
    SafeDumper.add_representer(
        type(None),
        lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:null", ""),
    )


def make_yaml(input_data, file_name):
    tweak_yaml()
    data_str = json.dumps(input_data, sort_keys=True)
    data = json.loads(data_str, object_pairs_hook=collections.OrderedDict)
    with open(file_name, "w") as out:
        yaml.safe_dump(
            data, out, indent=2, default_flow_style=False, allow_unicode=True
        )


@cli.command()
@click.option("--mapping", help="ES Mapping file")
@click.option("--schema", help="JSON Schema file")
@click.option("--filename", help="Output file name for the fields file")
def generate(mapping, schema, filename):
    mapping = json.load(open(mapping))
    schema = json.load(open(schema))
    schema.pop("$schema", 0)

    # Index for which keys need to be marked as having exact counterparts
    exact_index = get_exact_fields(mapping)
    # Index to lookup a non-array version of key name (so it maps to ES)
    es_items_index = generate_es_to_schema_index(schema)
    # All the fields to process from the schema
    schema_fields = get_schema_fields(schema)

    for field in schema_fields:
        type_field = field + "." + "type"
        # No need expand these complex types with DEFAULT attributes
        if get_deep(schema, type_field) in ["object", "array"]:
            continue
        if not field:
            continue

        es_field = es_items_index.get(field, None)
        is_exact = True if es_field in exact_index else False

        # Expand each of the keys to match DEFAULTS.
        # set_deep() will not overwrite data if it does exist, but will add if it
        # does not
        for key, value in DEFAULTS.items():
            full_key = field + "." + key
            if is_exact and key == "is_exact":
                value = True

            set_deep(schema, full_key, value)

    make_yaml(schema, filename)


@cli.command()
@click.option(
    "--host", help="Base URL for the API to test", default="http://localhost:8000"
)
@click.option("--mapping", help="ES Mapping file")
@click.option("--endpoint", help="Endpoint to test, i.e. /drug/event")
def test_api_keys(host, mapping, endpoint):
    """Utility that tests an API to make sure that all fields in the mapping
    file are queryable via the API. It will spit a list of non 200 keys to
    console.

    Please note, it is possible to get false positives, since the API responds
    with 404s when there is no data in the response. For instance, the query
    `/device/510k.json?search=_exists_:ssp_indicator` returns nothing, but it
    is a valid field from the source, it just happens to never have data.
    """
    mapping = json.load(open(mapping))
    exact_index = get_exact_fields(mapping)
    flat_map = get_mapping_fields(mapping)
    url = "%s%s.json?search=_exists_:%s"
    for key in flat_map.keys():
        # Remove non queryable fields from key name
        key = key.replace("properties.", "").replace(".fields", "")
        responses = []
        responses.append((requests.get(url % (host, endpoint, key)).status_code, key))

        if key in exact_index:
            key = key + ".exact"
            responses.append(
                (requests.get(url % (host, endpoint, key)).status_code, key)
            )

        missing_keys = []
        for resp, req in responses:
            if resp != 200:
                missing_keys.append(req)
            else:
                logging.info("%s: Ok!", req)

    if missing_keys:
        logging.info("The following keys returned a non 200 response: %s", missing_keys)
    else:
        logging.info("All keys in the mapping are returned by API")


@cli.command()
@click.option("--mapping", help="ES Mapping file")
def flat_mapping(mapping):
    mapping = json.load(open(mapping))
    flat_map = get_mapping_fields(mapping)
    for key in sorted(flat_map.keys()):
        # Remove non queryable fields from key name
        key = key.replace("properties.", "").replace(".fields", "")
        if "exact" not in key:
            print(key)


if __name__ == "__main__":
    logging.basicConfig(
        format="%(created)f %(filename)s:%(lineno)s [%(funcName)s] %(message)s",
        level=logging.INFO,
        stream=sys.stderr,
    )

    cli()
