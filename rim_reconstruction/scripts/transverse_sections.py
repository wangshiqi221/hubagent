"""python -m rim_reconstruction.scripts.transverse_sections --help"""
import argparse
import json
from ..services.transverse_service import process_transverse_sections


def main():
    parser=argparse.ArgumentParser(description='Python-only transverse STL sections using an existing hub axis')
    parser.add_argument('--axis-call-directory',required=True)
    parser.add_argument('--depths',nargs='+',type=float,required=True,help='Signed millimetres from axis origin')
    parser.add_argument('--tolerance',type=float,default=1e-5)
    args=parser.parse_args()
    result=process_transverse_sections({'axis_call_directory':args.axis_call_directory,
        'depths_mm':args.depths,'tolerance_mm':args.tolerance})
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))


if __name__=='__main__':
    main()
