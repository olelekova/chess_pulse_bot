#!/bin/bash
set -e
apt-get update -y
apt-get install -y stockfish
pip install -r requirements.txt
