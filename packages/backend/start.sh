#!/bin/bash

uv run geospatial-atlas spawn99/wine-reviews --text description --split train --static ../viewer/dist --cors http://localhost:5173 --mcp "$@"
