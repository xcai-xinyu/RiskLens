"""Offline review entry point: explicit candidate version, output-bound human ledger."""
import argparse
import json
from pathlib import Path

from evaluation.candidate_data import load_candidate_dataset
from evaluation.candidate_scoring import review_template
from evaluation.scorers import score


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--case-id', required=True)
    parser.add_argument('--output', required=True, help='Actual answer JSON; no model call is made')
    parser.add_argument('--evidence', required=True, help='Actual runtime evidence mapping JSON')
    parser.add_argument('--review', help='Completed human ledger, bound to this exact output')
    parser.add_argument('--write-template', help='Create a pending ledger; refuses overwrite')
    parser.add_argument('--result', required=True, help='Create score JSON; refuses overwrite')
    args = parser.parse_args()
    dataset = load_candidate_dataset(args.dataset)
    case = dataset.cases[args.case_id]
    output = json.loads(Path(args.output).read_text())
    evidence = json.loads(Path(args.evidence).read_text())
    review = json.loads(Path(args.review).read_text()) if args.review else None
    result = score(case, output, evidence, human_review=review)
    result['data_version'] = dataset.manifest['data_version']
    destinations = [Path(p) for p in (args.result, args.write_template) if p]
    if any(p.exists() for p in destinations) or len(set(destinations)) != len(destinations):
        raise FileExistsError('review_output_already_exists_or_duplicate')
    if args.write_template:
        with Path(args.write_template).open('x') as stream:
            json.dump(review_template(case, output, evidence), stream, ensure_ascii=False, indent=2)
    with Path(args.result).open('x') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'case_id': args.case_id, 'status': result['status'],
                      'scorer_version': result['scorer_version'], 'real_model_calls': 0}))


if __name__ == '__main__':
    main()
