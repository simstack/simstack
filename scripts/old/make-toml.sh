#!/bin/bash
cp /app/simstack-docker.toml simstack.toml

sed -i "s|XXXCONNECTION_STRINGXXX|${CONNECTION_STRING}|g" simstack.toml
sed -i "s|XXXDATABASEXXX|${DATABASE}|g" simstack.toml
sed -i "s|XXXTEST_DATABASEXXX|${TEST_DATABASE}|g" simstack.toml
sed -i "s|XXXEXTERNAL_SOURCE_DIRXXX|${EXTERNAL_SOURCE_DIR}|g" simstack.toml

cat simstack.toml
