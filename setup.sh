#!/bin/bash

if [ ! -f .gitmodules ]; then
    echo "No .gitmodules file found. Exiting."
    exit 1
fi

sed -i 's|https://github.com/|git@github.com:|g' .gitmodules

git submodule sync

git submodule update --init --recursive

echo "Submodule URLs converted to SSH successfully!"

# git clone git@github.com:organization-name/repo-name.git
# chmod +x setup.sh
# ./setup.sh
