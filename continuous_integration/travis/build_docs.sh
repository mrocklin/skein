#!/usr/bin/env bash
set -xe

conda install sphinx numpydoc;
pip install sphinxcontrib.autoprogram;

cd ~/skein/docs
make html
