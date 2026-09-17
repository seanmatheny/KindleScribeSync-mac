#!/bin/sh
# Build bin/scribe-ocr, the handwriting recognition helper.
# Requires the Xcode Command Line Tools (xcode-select --install).
set -eu

cd "$(dirname "$0")/.."
mkdir -p bin
swiftc -O -o bin/scribe-ocr ocr/scribe-ocr.swift
echo "Built bin/scribe-ocr"
