"""Prepare and validate humanizer messages using the current harness as writer.

No API calls, CLI model processes, or Telegram sends. JSON requests use the
humanizer CommunicationRequest fields. Local artifacts belong in .outbox/.
"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from humanizer_framework import CommunicationFramework
from humanizer_framework.models import CommunicationRequest, Message, StyleConstraints, VoiceProfile
from humanizer_framework.validators import normalize_output, validate_output

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def make_request(data):
    values = dict(data)
    values["conversation"] = [Message(**m) for m in values.get("conversation", [])]
    if values.get("constraints") is not None:
        values["constraints"] = StyleConstraints(**values["constraints"])
    if values.get("voice") is not None:
        values["voice"] = VoiceProfile(**values["voice"])
    return CommunicationRequest(**values)


def prepare(data):
    request = make_request(data)
    package = CommunicationFramework().prepare(request)
    return request, package


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--request", required=True)
    prep.add_argument("--out", required=True)
    check = commands.add_parser("validate")
    check.add_argument("--package", required=True)
    check.add_argument("--text-file", required=True)
    check.add_argument("--out", required=True, help="validated text; removed if validation fails")
    args = parser.parse_args()

    if args.command == "prepare":
        data = read_json(args.request)
        _, package = prepare(data)
        bundle = {"request": data, "package": asdict(package)}
        write_json(args.out, bundle)
        print(json.dumps(bundle["package"], ensure_ascii=False, indent=2))
        return

    # Fail closed: an old success must never survive a failed revalidation.
    output = Path(args.out)
    if output.resolve() in {Path(args.package).resolve(), Path(args.text_file).resolve()}:
        parser.error("--out must differ from input files")
    output.unlink(missing_ok=True)
    bundle = read_json(args.package)
    request, package = prepare(bundle["request"])
    current = json.loads(json.dumps(asdict(package)))
    if current != bundle["package"]:
        raise ValueError("Prepared instructions changed; run prepare again before writing")
    raw = Path(args.text_file).read_text(encoding="utf-8")
    text = normalize_output(raw, package.constraints, request.language)
    issues = validate_output(text, request, package.plan, package.constraints)
    valid = not any(issue.hard for issue in issues)
    result = {"valid": valid, "text": text, "issues": [asdict(i) for i in issues],
              "llm_grader": "not_run"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not valid:
        raise SystemExit(2)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
