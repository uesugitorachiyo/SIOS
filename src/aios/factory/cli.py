"""Small evidence CLI. Model orchestration is frozen legacy."""
import argparse
import json
import sys
from pathlib import Path
from .contract import Blocked

LEGACY = {'inspect','draft-group','prepare-design','run-design','collect-controls','assemble-qualification','run','status'}

def parser():
    p=argparse.ArgumentParser(prog='sios-factory',description='Local prepare/check/report evidence; trusted developer code. No GitHub writes. Old model/qualification commands are unsupported frozen legacy.')
    sub=p.add_subparsers(dest='command',required=True)
    prepare=sub.add_parser('prepare',help='Copy request and isolated baseline/work; no models')
    prepare.add_argument('--request',required=True)
    prepare.add_argument('--baseline-dir',required=True)
    prepare.add_argument('--run-dir',required=True)
    prepare.add_argument('--predecessor',help='Known-ended failed check/finalization or validated FAIL/UNKNOWN review; fresh identity/budget')
    prepare.add_argument('--bind-tools',action='store_true',help='Hash omitted Python/Git pins for explicitly selected absolute paths')
    check=sub.add_parser('check',help='Consume once; capture full diff and run fixed trusted tests')
    check.add_argument('--run-dir',required=True)
    report=sub.add_parser('report',help='Verify exact evidence and optionally import external independent review')
    report.add_argument('--run-dir',required=True)
    report.add_argument('--review-receipt')
    return p


def _summary(command, value, run_dir=None):
    state = value.get('state')
    if command == 'prepare' and state == 'prepared':
        confirmed=['isolated baseline and work prepared']; missing=['checks','independent review']; action='Edit only the allowed files in the emitted work directory, then run check.'
    elif command == 'check' and value.get('passed') is True:
        confirmed=['check passed with recorded process and test evidence']; missing=['human criteria and independent review']; action='Obtain an independent review of the recorded packet.'
    elif state == 'PASS':
        confirmed=['check and bound four-facet review recorded']; missing=[]; action='Retain the immutable run and its evidence.'
    elif state == 'review_import_incomplete':
        confirmed=['review import is incomplete or invalid']; missing=['valid independent review']; action='Stop and preserve all files; do not reimport or switch sessions.'
    elif state == 'held_unknown' or value.get('known_ended') is False:
        confirmed=['termination is unknown']; missing=['known termination','independent review']; action='Stop; do not restart or switch the collection.'
    elif state == 'restart_required' or (command == 'check' and value.get('passed') is False):
        confirmed=['check did not pass']; missing=['passing check','independent review']; action='Preserve this run; use a fresh eligible predecessor only after known end.'
    elif value.get('verdicts'):
        confirmed=['bound review recorded']; missing=['all review facets passing']; action='Preserve this run; a validated negative review may be an explicit fresh predecessor.'
    else:
        confirmed=[]; missing=['independent review' if value.get('missing') else 'verified completion']; action='Inspect the recorded reason and evidence; do not retry consumed work.'
    evidence = {}
    if run_dir is not None:
        root=Path(run_dir).absolute()
        for key,name in (('request','request.json'),('prepared','PREPARED.json'),('check','CHECK.json'),
                         ('test_result','TEST_RESULT.json'),('review_packet','REVIEW_PACKET.json')):
            path=root/name
            if path.is_file() and not path.is_symlink(): evidence[key]=str(path)
    return {'outcome':state or ('check_passed' if value.get('passed') is True else 'unmet'),
            'confirmed':confirmed,'unmet_unknown':missing,'next_safe_action':action,'evidence':evidence}

def main(argv=None):
    args_list=list(sys.argv[1:] if argv is None else argv)
    if args_list and (args_list[0] in LEGACY or '--local-profile' in args_list):
        print(json.dumps({'state':'unmet','reason':'unsupported_frozen_legacy'}),file=sys.stderr)
        return 2
    args=parser().parse_args(args_list)
    from . import simple
    try:
        if args.command=='prepare':
            value=simple.prepare(args.request,args.baseline_dir,args.run_dir,args.predecessor,bind_tools=args.bind_tools)
        elif args.command=='check':value=simple.check(args.run_dir)
        else:value=simple.report(args.run_dir,args.review_receipt)
        value['summary']=_summary(args.command,value,args.run_dir)
        print(json.dumps(value,indent=2,allow_nan=False))
        return 0 if value.get('state') in ('prepared','PASS') or value.get('passed') is True else 2
    except (Blocked,OSError,ValueError,KeyError,TypeError,KeyboardInterrupt) as exc:
        value={'state':'unmet','reason':exc.code if isinstance(exc,Blocked) else type(exc).__name__,
               'action':'retain run; never retry consumed checks/imports; explicit predecessor supports only known-ended failed check/finalization or a validated negative independent review; missing or tampered evidence needs operator resolution'}
        if isinstance(exc, Blocked) and exc.code == 'collection_incomplete_or_unknown':
            value.update(state='held_unknown', action='The collection is held; preserve it and do not retry or switch parents until termination is independently established.')
        value['summary']=_summary(args.command,value)
        print(json.dumps(value),file=sys.stderr)
        return 2

if __name__=='__main__':
    raise SystemExit(main())
