"""Local server administration. Secrets go to protected files, never stdout."""
import argparse
import json
import os
from pathlib import Path
from accounts import Accounts


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('invite','signup-open','signup-close'))
    parser.add_argument('--email');parser.add_argument('--output');parser.add_argument('--workspace-id',default='');args=parser.parse_args()
    root=Path(os.getenv('DRIVECAST_DATA_DIR','/data'))/'accounts'
    manager=Accounts(root,os.environ['DRIVECAST_ORIGIN'],hosted=True)
    if args.action=='invite':
        if not args.email or not args.output: raise ValueError('Email and private output path are required.')
        token=manager.invite(args.email,args.workspace_id)
        destination=Path(args.output);destination.write_text(manager.origin+'/login#invite='+token);destination.chmod(0o600)
    else:
        if args.action=='signup-open' and not os.getenv('DRIVECAST_SMTP_HOST'):
            raise ValueError('Configure and test SMTP email verification before opening public registration.')
        with manager.db() as db: db.execute("INSERT OR REPLACE INTO account_settings VALUES('public_signup',?)",('1' if args.action=='signup-open' else '0',))


if __name__=='__main__': main()
