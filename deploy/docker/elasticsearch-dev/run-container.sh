#!/bin/bash

docker stop elasticsearch-dev
docker rm -f elasticsearch-dev
docker rmi -f openfda/elasticsearch.0

set -x
set -e

sudo docker build -t openfda/elasticsearch.0 .
sudo docker run \
  -d \
  -v ${ES_DATA_PATH:-/media/ebs/}:/data0\
  -p 9200:9200\
  -p 9300:9300\
  -e ES_JAVA_OPTS="$ES_JAVA_OPTS"\
  --name elasticsearch-dev\
  openfda/elasticsearch.0
