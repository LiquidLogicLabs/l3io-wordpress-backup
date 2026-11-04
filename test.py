#!/usr/bin/python3 

""" Run all the unit tests. """

from subprocess import CalledProcessError, check_call
import logging
import os
import pathlib

logging.basicConfig(level=logging.DEBUG)

# tests/classes import the 0.2.21 package, which now lives in legacy/ so that it
# no longer shadows `python -m wpbackup2`.
LEGACY = str(pathlib.Path(__file__).resolve().parent / 'legacy')

try:
    check_call(['coverage',
                'run',
                '-m',
                'unittest',
                'discover',
                '--start-directory',
                'tests'],
               env={**os.environ, 'PYTHONPATH': LEGACY})
except CalledProcessError:
    exit(1)

#check_call(['coverage', 'report'])
