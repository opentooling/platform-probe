"""Turn CRDs into JSON schemas kubeconform can validate against.

    python3 hack/crd2schema.py <out-dir> <crd.yaml>...

Writes <out-dir>/<kind>_<version>.json for every served version. Objects get
additionalProperties: false, as kubeconform's own converter does, so a misspelt
field fails validation instead of being ignored.
"""

import json
import sys

import yaml


def strict(schema):
    if isinstance(schema, dict):
        if (
            schema.get("type") == "object"
            and "properties" in schema
            and "additionalProperties" not in schema
            and not schema.get("x-kubernetes-preserve-unknown-fields")
        ):
            schema["additionalProperties"] = False
        for value in schema.values():
            strict(value)
    elif isinstance(schema, list):
        for value in schema:
            strict(value)


def main(out_dir, paths):
    for path in paths:
        with open(path) as f:
            crd = yaml.safe_load(f)
        kind = crd["spec"]["names"]["kind"].lower()
        for version in crd["spec"]["versions"]:
            schema = version["schema"]["openAPIV3Schema"]
            strict(schema)
            # Not at the root: some CRDs leave apiVersion, kind and metadata out of it.
            schema.pop("additionalProperties", None)
            out = f"{out_dir}/{kind}_{version['name']}.json"
            with open(out, "w") as f:
                json.dump(schema, f)
            print(out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
