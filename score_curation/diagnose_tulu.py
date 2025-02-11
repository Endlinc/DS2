import sys
import os
o_path = os.getcwd()
sys.path.append(o_path) # set path so that modules from other foloders can be loaded

import torch
import argparse

from docta.utils.config import Config
from docta.datasets import TULU_RLHF
from docta.core.preprocess import Preprocess
from docta.datasets.data_utils import load_embedding
torch.multiprocessing.set_sharing_strategy('file_system')

def parse_args():
    parser = argparse.ArgumentParser(description='Train a classifier')
    parser.add_argument('--config', help='train config file path', default='tulu_template.py')
    parser.add_argument('--dataset_name', help='tulu subset name', default='flan_v2')
    parser.add_argument('--rating_model', help='model full name', default='meta-llama/Meta-Llama-3.1-8B-Instruct')


    args = parser.parse_args()
    return args



'''load data'''
args = parse_args()
cfg = Config.fromfile(args.config)
cfg.data_root = f'../'
cfg.file_name = args.dataset_name
cfg.dataset_type = args.dataset_name
print(f"###### Dataset: {args.dataset_name}  #### Rating model: {args.rating_model}")

cfg.save_path = f'./results/{args.rating_model}/{args.dataset_name}/'
cfg.preprocessed_dataset_path = cfg.save_path + f'dataset_{args.dataset_name}.pt'

cfg.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


## raw labels 
cfg.label_path = cfg.data_root + f'model_finetune/selected_data/{args.rating_model}/{args.dataset_name}/output_labels_revised.pt'



dataset = TULU_RLHF(cfg, args, train=True)

test_dataset = None
print(f'TULU sub-dataset {args.dataset_name} load finished')


'''preprocess data'''
pre_processor = Preprocess(cfg, dataset, test_dataset)
pre_processor.encode_feature()
print(pre_processor.save_ckpt_idx)


data_path = lambda x: cfg.save_path + f'embedded_{cfg.dataset_type}_{x}.pt'
dataset, _ = load_embedding(pre_processor.save_ckpt_idx, data_path, duplicate=True) ## duplicate dataset


'''detect data'''
from docta.apis import DetectLabel, DetectFeature
from docta.core.report import Report
report = Report()

#score-wise: score curation technique
detector = DetectLabel(cfg, dataset, report = report)
detector.detect()



## feature-wise: embedding distance
print("starting feature-wise part: calculating embedding distance!!!")
detector_feature = DetectFeature(cfg, dataset, report = report)
detector_feature.rare_score()


##store reports
report_path = cfg.save_path + f'{cfg.dataset_type}_report.pt'
torch.save(report, report_path)
print(f'Report saved to {report_path}')

