import torch
import fire
import os
import json
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
import accelerate
from functools import partial
from torch.utils.data import DataLoader,Dataset
# from datasets import load_dataset, Dataset
from tqdm import tqdm
import numpy as np
from accelerate.utils import is_xpu_available
# from typing import Iterable, List, Optional, Tuple
from accelerate import Accelerator
import re
from datasets import load_dataset
import sys
import gc
print("Torch version:", torch.__version__)

### store the model 
os.environ["TOKENIZERS_PARALLELISM"] = "false"
B_INST, E_INST = "[INST]", "[/INST]"


class CustomDataset(Dataset):
    def __init__(self, dataset_name, dialogs, template):
        # 直接存储原始对话数据
        self.dataset_name = dataset_name
        self.dialogs = dialogs
        self.template = template
    def __getitem__(self, idx):

        # return self.dialogs[idx]

        return {'data': self.dialogs[idx], 'index': idx}
    
    def __len__(self):
        return len(self.dialogs)
    
    def map(self, function):
        self.dialogs = [function(item, self.template) for item in self.dialogs]
        return self
    


def read_dialogs_from_json(file: str,  data_size):
    dialogs = []
    # The number of dialogs
    # halueval
    # end_dialog_idx = 1010
    # start_dialog_idx = 10
    
    start_dialog_idx = 0
    # end_dialog_idx = 10000

    end_dialog_idx = data_size
    
    dialog_idx = 0  # Start counting from 0 to correctly match the first dialog as index 1
    with open(file, 'r') as f:
        for line in f:
            # Skip comment lines
            if not line.strip() or line.strip().startswith("//"):
                continue  # Skip the line if it's a comment or blank


            if start_dialog_idx <= dialog_idx <  end_dialog_idx:
                # This point can use pdb for debugging or directly process the data
                dialog_data = json.loads(line)
                user_query = dialog_data["user_query"]
                chatgpt_response = dialog_data['chatgpt_response']
                hallucination_label = dialog_data['hallucination']  # 'yes' or 'no'
                # hallucination_spans = dialog_data.get('hallucination_spans', [])  # Use get to handle missing fields

                # Construct the dialog dictionary
                dialog = [{
                    # "role": "user",
                    "content": user_query,
                    "chatgpt_response": chatgpt_response,
                    "hallucination_label": hallucination_label,
                    # "hallucination_spans": hallucination_spans
                }]
                
                dialogs.append(dialog)
            elif dialog_idx > end_dialog_idx:
                # Stop reading the file if the end of the target range is reached
                break
            
            
            dialog_idx += 1  # Increment dialog index only for non-comment lines

    return dialogs


def create_prompt_formats(dialog, template):
    """
    Format various fields of the dialog ('content', 'chatgpt_response')
    Then concatenate them using two newline characters: pre_prompt & post_prompt
    """

    if template == 1: # chale
        #prompt template 1 
        
        pre_prompt = 'Question:'
        post_prompt = '\n\nPlease directly give the answer, followed by only one sentence to briefly and shortly describe the relevant information (less than 15 words).'
        
    elif template == 2:#truthfulqa, halueval
        #prompt template 2
        pre_prompt = 'Please directly provide the answer for the following question.\n\nQuestion: '
        post_prompt = '\nAnswer:'
        
    elif template == 3:
        # prompt template 3
        pre_prompt = 'Question:'
        post_prompt = '\n\nAnswer:'

    elif template == 4:
        #blank template
        pre_prompt = ''
        post_prompt = ''
    
    target_answer_length =3
    # typical question-answering type
    if template <=4:
        formatted_prompt = f"{pre_prompt}{dialog['content']}{post_prompt}{' '.join(dialog['chatgpt_response'].split()[:target_answer_length])}"
        # formatted_prompt = f"{B_INST}{pre_prompt}{dialog['content']}{post_prompt}{E_INST}"


    #special case: dialogue type
    if template == 5:
        background = "###Knowledge: "
        pre_prompt = '###Dialogue History: '
        post_prompt = '###Please answer the last question from human according to the Knowledge and dialogue history.\n\nAnswer:'
        
        formatted_prompt = f"{B_INST}{background + dialog['knowledge']}\n{pre_prompt + dialog['content']}\n{post_prompt}{E_INST}"
    
    
    if template == 6: #openmath dataset
        # prompt template 3
        pre_prompt = 'Please solve this question using Python code.  Question:'
        post_prompt = '\n\nAnswer:'
        
        formatted_prompt = f"{B_INST}{pre_prompt}{dialog['content']}{post_prompt}{E_INST}"

        
    
    dialog["content"] = formatted_prompt

    return dialog


def prompting(model_name):
    if model_name == 'gemma':
        prompt = (
            "As a conversation quality evaluator, your task is to assess the quality of the conversation below. "
            "Rate the conversation on a scale from 0 to 5 considering the factors of coherence, relevance, and informativeness. "
            "A rating of 0 means the conversation is of very low quality, and a rating of 5 means it is of very high quality.\n\n"
            "Rubric for Evaluation:\n\n"
            "1. Coherence:\n"
            "   - 0-1: The answer is incoherent or difficult to understand.\n"
            "   - 2-3: The answer is somewhat coherent but may contain unclear segments.\n"
            "   - 4-5: The answer is fully coherent, logical, and easy to understand.\n\n"
            "2. Relevance:\n"
            "   - 0-1: The answer is only partially relevant to the user's query.\n"
            "   - 2-3: The answer is mostly relevant but includes some unrelated information.\n"
            "   - 4-5: The answer is completely relevant and directly addresses the user's query.\n\n"
            "3. Informativeness:\n"
            "   - 0-1: The answer provides minimal useful information.\n"
            "   - 2-3: The answer provides some useful information but lacks detail.\n"
            "   - 4-5: The answer is highly informative and provides detailed and valuable information.\n\n"
            "Here are examples for reference:\n\n"
            "Example 1 (Basic Quality - Score: 1):\n"
            "Conversation:\n"
            "User: Can you explain the Pythagorean theorem?\n"
            "Assistant: Pythagorean theorem says that in a right-angled triangle, the square of the hypotenuse is equal to the sum of the squares of the other two sides. The formula is a^2 + b^2 = c^2, where c is the hypotenuse and a and b are the other two sides.\n"
            "## Rating: 1\n\n"
            "Example 2 (High Quality - Score: 4):\n"
            "Conversation:\n"
            "User: Can you explain the Pythagorean theorem?\n"
            "Assistant: The Pythagorean theorem is a fundamental principle in geometry, named after the ancient Greek mathematician Pythagoras. This theorem states that in a right-angled triangle, the square of the length of the hypotenuse (the side opposite the right angle) is equal to the sum of the squares of the lengths of the other two sides. Mathematically, this relationship is expressed as a^2 + b^2 = c^2, where c represents the hypotenuse, and a and b are the lengths of the triangle's other two sides. This theorem is crucial in various fields, including mathematics, physics, engineering, and computer science, because it provides a method to calculate the distance between two points in a plane. For example, if one side of the triangle is 3 units long and the other side is 4 units long, the hypotenuse will be 5 units long, as 3^2 + 4^2 = 5^2.\n"
            "## Rating: 4\n\n"
            "Now, please evaluate the following conversation:"
            )
    elif model_name == 'llama':
        prompt = (
        "As a conversation quality evaluator, your task is to assess the quality of the conversation below. "
        "Rate the conversation on a scale from 0 to 5 considering the factors of coherence, relevance, and informativeness. "
        "A rating of 0 means the conversation is with a low quality, and a rating of 4 means it is pretty good.\n\n"
        "Here is an example:\n"
        "Conversation:\n"
        "User: What is the capital of France?\n"
        "Assistant: The capital of France is Paris.\n"
        "## Rating: 1\n\n"
        "Now, please evaluate the following conversation and return the numerical (integer) rating score without explanations.\n"
        )
        # prompt = (
        #     "As a conversation quality evaluator, your task is to assess the quality of the conversation below. "
        #     "Rate the conversation on a scale from 0 to 5 considering the factors of coherence, relevance, and informativeness. "
        #     "A rating of 0 means the conversation is of very low quality, and a rating of 5 means it is of very high quality.\n\n"
        #     "Rubric for Evaluation:\n\n"
        #     "1. Coherence:\n"
        #     "   - 0-1: The answer is incoherent or difficult to understand.\n"
        #     "   - 2-3: The answer is somewhat coherent but may contain unclear segments.\n"
        #     "   - 4-5: The answer is fully coherent, logical, and easy to understand.\n\n"
        #     "2. Relevance:\n"
        #     "   - 0-1: The answer is only partially relevant to the user's query.\n"
        #     "   - 2-3: The answer is mostly relevant but includes some unrelated information.\n"
        #     "   - 4-5: The answer is completely relevant and directly addresses the user's query.\n\n"
        #     "3. Informativeness:\n"
        #     "   - 0-1: The answer provides minimal useful information.\n"
        #     "   - 2-3: The answer provides some useful information but lacks detail.\n"
        #     "   - 4-5: The answer is highly informative and provides detailed and valuable information.\n\n"
        #     "Here are examples for reference:\n\n"
        #     "Example 1 (Basic Quality - Score: 1):\n"
        #     "Conversation:\n"
        #     "User: Can you explain the Pythagorean theorem?\n"
        #     "Assistant: Pythagorean theorem says that in a right-angled triangle, the square of the hypotenuse is equal to the sum of the squares of the other two sides. The formula is a^2 + b^2 = c^2, where c is the hypotenuse and a and b are the other two sides.\n"
        #     "## Rating: 1\n\n"
        #     "Example 2 (High Quality - Score: 4):\n"
        #     "Conversation:\n"
        #     "User: Can you explain the Pythagorean theorem?\n"
        #     "Assistant: The Pythagorean theorem is a fundamental principle in geometry, named after the ancient Greek mathematician Pythagoras. This theorem states that in a right-angled triangle, the square of the length of the hypotenuse (the side opposite the right angle) is equal to the sum of the squares of the lengths of the other two sides. Mathematically, this relationship is expressed as a^2 + b^2 = c^2, where c represents the hypotenuse, and a and b are the lengths of the triangle's other two sides. This theorem is crucial in various fields, including mathematics, physics, engineering, and computer science, because it provides a method to calculate the distance between two points in a plane. For example, if one side of the triangle is 3 units long and the other side is 4 units long, the hypotenuse will be 5 units long, as 3^2 + 4^2 = 5^2.\n"
        #     "## Rating: 4\n\n"
        #     "Now, please evaluate the following conversation:"
        #     )
    elif model_name == 'mistral':
        prompt = (
            "As a conversation quality evaluator, your task is to assess the quality of the conversation below. "
            "Rate the conversation on a scale from 0 to 5 considering the factors of coherence, relevance, and informativeness. "
            "A rating of 0 means the conversation is of very low quality, and a rating of 5 means it is of very high quality.\n\n"
            "Now, please evaluate the following conversation and return the numerical (integer) rating score with a brief explanation (two or three sentences).\n"
            )
        
    elif model_name == 'opt':
        prompt = ("xx")
    elif model_name == 'gpt':
        prompt = ("xx")
    else:
        prompt = (
        "As a conversation quality evaluator, your task is to assess the quality of the conversation below. "
        "Rate the conversation on a scale from 0 to 10 considering the factors of coherence, relevance, and informativeness. "
        "A rating of 0 means the conversation is with a low quality, and a rating of 2 means it is pretty good.\n\n"
        "Here is an example:\n"
        "Conversation:\n"
        "User: What is the capital of France?\n"
        "Assistant: The capital of France is Paris.\n"
        "## Rating: 9\n\n"
        "Now, please evaluate the following conversation:\n"
        )
    return prompt



def main(
    model_name: str = "llama",
    dataset_name: str = 'flan_v2',
    subset_name: str = None,
    prompt_template = 4, ### the prompt template
    data_size = 3000,
    peft_model: str=None,
    quantization: bool=False,
    max_new_tokens =128, #The maximum numbers of tokens to generate
    min_new_tokens:int=0, #The minimum numbers of tokens to generate
    prompt_file: str=None,
    seed: int=42, #seed value for reproducibility
    token_gap: int=0,
    root_path: str='logs',
    gpu_id: int=None,
    safety_score_threshold: float=0.5,
    do_sample: bool=True, #Whether or not to use sampling ; use greedy decoding otherwise.
    use_cache: bool=True,  #[optional] Whether or not the model should use the past last key/values attentions Whether or not the model should use the past last key/values attentions (if applicable to the model) to speed up decoding.
    top_p: float=0.9, # [optional] If set to float < 1, only the smallest set of most probable tokens with probabilities that add up to top_p or higher are kept for generation.
    temperature: float=1.0, # [optional] The value used to modulate the next token probabilities.
    top_k: int=50, # [optional] The number of highest probability vocabulary tokens to keep for top-k-filtering.
    repetition_penalty: float=1.2, #The parameter for repetition penalty. 1.0 means no penalty.
    length_penalty: int=1, #[optional] Exponential penalty to the length that is used with beam-based generation.
    enable_azure_content_safety: bool=False, # Enable safety check with Azure content safety api
    enable_sensitive_topics: bool=False, # Enable check for sensitive topics using AuditNLG APIs
    enable_saleforce_content_safety: bool=True, # Enable safety check woth Saleforce safety flan t5
    use_fast_kernels: bool = False, # Enable using SDPA from PyTorch Accelerated Transformers, make use Flash Attention and Xformer memory-efficient kernels
    enable_llamaguard_content_safety: bool = False,
    target_token_idx: int = 0, 
    top_g: int=5,
    replace_first_token: bool= False,
    output_dir="/mnt/azureml/crunch/outputs/",
    **kwargs
):

    # Set the seeds for reproducibility
    if is_xpu_available():
        torch.xpu.manual_seed(seed)
    else:
        torch.cuda.manual_seed(seed)
    torch.manual_seed(seed)

    '''load model & tokenizer'''
    # model_name = "meta-llama/Llama-2-7b-chat-hf"
    # flash attention 2
    # torch.bfloat16
    # 8bit 4bit bitsandbytes
    # accelerate
    
    if 'llama' in model_name:
        # model_full_name = "meta-llama/Llama-2-7b-chat-hf" #batch_size 25
        # model_full_name = "meta-llama/Meta-Llama-3-8B-Instruct"
        model_full_name = "meta-llama/Meta-Llama-3.1-8B-Instruct"
        # model_full_name = "meta-llama/Meta-Llama-3-70B-Instruct"
        batch_size = 25

    elif 'mistral' in model_name:
        model_full_name = 'mistralai/Mistral-7B-Instruct-v0.3'
        # model_full_name = 'mistralai/Mixtral-8x7B-Instruct-v0.1'
        batch_size = 50
        
    elif 'gemma' in model_name:
        model_full_name = 'google/gemma-2b-it' # batch_size 20
        # model_full_name = 'google/gemma-7b-it' #batch_size 10
        # model_full_name = 'google/gemma-2-9b-it' #batch_size 10
        batch_size=10

        
    elif model_name == 'opt':
        model_full_name = 'facebook/opt-6.7b'

    else:
        raise NotImplementedError
    
    print(f'####### Loading LLM model: {model_full_name}')
    print(f'####### Datset: {dataset_name}')
    print(f'####### Batch size: {batch_size}')


    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
    )

    accelerator = Accelerator()
    # import pdb;pdb.set_trace()

    device_map=f'cuda:{gpu_id}' if gpu_id is not None else 'auto'
    model = AutoModelForCausalLM.from_pretrained(
        model_full_name,
        torch_dtype=torch.bfloat16,
        quantization_config = bnb_config,
        # attn_implementation="flash_attention_2",  # 假设模型支持这个参数
        # device_map="balanced",#"auto", "balanced", "balanced_low_0", "sequential"
        # device_map="auto", # when you use the accelerator, you don't need to set device_map
        # device_map={'':torch.cuda.current_device()},
        # device_map = {"": accelerator.device},
    )
    model.bfloat16()
    tokenizer = AutoTokenizer.from_pretrained(model_full_name, padding_side='left')
    tokenizer.pad_token_id = tokenizer.eos_token_id
    # tokenizer.add_special_tokens({'pad_token': '[PAD]'})




    '''prompting'''
    print("choose the prompt for model!!")
    # pre_prompt = prompting(model_name)

    pre_prompt = ('''
            We are evaluating data samples to determine their suitability for finetuning a language model (LLM). 
            As a data sample quality evaluator, your task is to assess the following data sample based on the criteria: Rarity, Complexity, Informativeness, Helpfulness.
            Rate the sample on a scale from 1 to 10 for each criterion, and then give an overall rating on a scale from 1 to 5.
            A rating of 1 means the sample is not suitable, and a rating of 5 means it is very suitable for finetuning.

            Now, please evaluate the following data sample and directly return the overall numerical (integer) rating score in the JSON format: {"### Rating": <number, 1-5>}.
            '''
            )


    # pre_prompt = (
    #     "We are evaluating data samples to determine their suitability for finetuning a language model (LLM). "
    #     "As a data sample quality evaluator, your task is to assess the following data sample based on the criteria: Rarity, Complexity, Informativeness, Helpfulness."
    #     "Rate the sample on a scale from 1 to 10 for each criterion, and then give an overall rating on a scale from 1 to 5.\n\n"
    #     "Here is an example:\n\n"
    #     "Sample content:\n"
    #     "The quick brown fox jumps over the lazy dog.\n\n"
    #     # "Ratings:\n"
    #     # "Rarity: 2\n"
    #     # "Completeness: 10\n"
    #     # "Informativeness: 3\n"
    #     "## Rating: 1\n\n"
    #     "Now, please evaluate the following data sample and directly return the overall numerical (integer) rating score without explanations.\n\n"
    #     )


    '''preprocess dataset'''
    print("Preprocessing dataset...")
    inputs= []
    ##########################################################################################
    # dataset_name = "allenai/tulu-v2-sft-mixture"
    # data = load_dataset(dataset_name, cache_dir=cache_dir)
    # dialogs = data['train'].select(range(10000))
    # dataset_name = 'flan_v2'
    data = load_dataset('parquet', data_files=f'./tulu_split_parquet/{dataset_name}.parquet')
    dialogs = data['train']

    # for tulu dataset
    for dialog in dialogs:
        conversation = ""
        for message in dialog['messages']:  #[{{'role': 'user', 'content': 'blabla'}, {'role': 'assistant', 'content': 'blabla'}]
            conversation += f"### {message['role']}: {message['content']}\n"
        # import pdb;pdb.set_trace()
        inputs.append(pre_prompt + conversation + "\n### Rating:")

    ##########################################################################################
    # dataset_name = "GAIR/lima"
    # dataset = load_dataset(dataset_name,cache_dir=cache_dir)
    # dialogs = dataset['train']
    
    # # for LIMA dataset
    # for dialog in dialogs:
    #     # import pdb;pdb.set_trace()
    #     for message in dialog['conversations']: 
    #         conversation = f"### User: {dialog['conversations'][0]} \n\n### Assistant: {dialog['conversations'][-1]}\n ### Rating:"
        
    #     inputs.append(pre_prompt + conversation)
    ##########################################################################################

    # dataset_name = "mosaicml/dolly_hhrlhf"
    # dataset = load_dataset(dataset_name,cache_dir=cache_dir)
    # dialogs = dataset['train'][:10000] 

    # for prompt, response in zip(dialogs['prompt'], dialogs['response']):
    #     conversation = f"### User: {prompt} \n\n### Assistant: {response}\n### Rating:"
    #     inputs.append(pre_prompt + conversation)
    # # # import pdb;pdb.set_trace()


    ##########################################################################################

    # dialogs = load_data(dataset_name, subset_name, data_size)
    dataset = CustomDataset(dataset_name, inputs, template=prompt_template)
    # dataset = dataset.map(create_prompt_formats)
    data_loader = DataLoader(dataset, batch_size=batch_size, shuffle=False) #, shuffle=True, seed=42 


    ###accelerator 
    data_loader, model, tokenizer = accelerator.prepare(data_loader, model, tokenizer)

    output_text_all = []
    output_labels = []
    results = [] #store the results for data parallel

    model.eval()
    rating_batch = []
    matching_patterns = [
                        r"\"### Rating\":(\d+)",
                        r"\"### Rating\": (\d+)",
                        r"Rating:(\d+)",
                        r"### Rating:#+ (\d+)",
                        r"The final answer is:.*?(\d+)",
                        r"### Rating:.*?(\d+)"] ##danger 
    matching_patterns_compiled = [re.compile(pattern) for pattern in matching_patterns]


    for batch in tqdm(data_loader, desc="Generating inference info for answers"):
        # import pdb; pdb.set_trace()

        batch_data = batch['data']
        batch_indices = batch['index'] #record the index for each sample 

        encodings = tokenizer(batch_data, padding=True, max_length=2048, truncation=True, return_tensors="pt")
        encodings = {k: v.to(accelerator.device) for k, v in encodings.items()}
        with torch.no_grad():
            outputs = accelerator.unwrap_model(model).generate(
                input_ids=encodings['input_ids'].to(accelerator.device),
                attention_mask=encodings['attention_mask'].to(accelerator.device),
                max_new_tokens=max_new_tokens,
                do_sample=do_sample,
                top_p=top_p,
                temperature=temperature,
                use_cache=use_cache,
                top_k=top_k,
                repetition_penalty=repetition_penalty,
                length_penalty=length_penalty,
                **kwargs
            )

            output_text_batch = [tokenizer.decode(x, skip_special_tokens=True) for x in outputs]
            # output_answer_text_batch = [tokenizer.decode(x[attention_mask.shape[1]:], skip_special_tokens=True) for x in outputs]

            rating_batch = []
            for i, output_text in enumerate(output_text_batch):
                print("########################################################################################\n")
                print(output_text)   
                print("\n########################################################################################")

                for pattern in matching_patterns_compiled:
                    match = pattern.search(output_text)
                    if match:
                        break
        
                if match and 0 <=int(match.group(1)) <=5:
                    rating = int(match.group(1)) 
                else:
                    rating = -1
                    
                # #extract rating score
                # match = re.search(r"### Rating: (\d+)", output_text)
                # rating = int(match.group(1)) if match else -1
                # # results.append((batch_indices[i], output_text, rating))
                rating_batch.append(rating)
                results.append((batch_indices[i], rating))

            print(f"$$$$$$$$$$$$$ rating batch (size: {len(rating_batch)}): {rating_batch}")
            print(f"$$$$$$$$$$$$$ rating batch's unlabel samples: {rating_batch.count(-1)}")

            import pdb;pdb.set_trace()


        del encodings, output_text_batch, batch
        # torch.cuda.empty_cache()
        # gc.collect()

    '''load parameters'''
    print('Storing parameters...')
    if subset_name is not None: 
        path = os.path.join(root_path, model_name, f"{dataset_name}-{subset_name}")
    else:
        path = os.path.join(root_path, model_name, dataset_name)

    if not os.path.exists(path):
        os.makedirs(path)
        
    # torch.save(output_text_all, path + f'/output_text_all.pt')
    # torch.save(output_labels, path + f'/output_labels.pt')


    #####################################################################################

    # Barrier to ensure all processes have finished saving
    accelerator.wait_for_everyone()
    
    # Convert results to tensors and move them to CUDA device
    indices_tensor = torch.tensor([x[0] for x in results], dtype=torch.long).to(accelerator.device)
    # text_tensor = torch.nn.utils.rnn.pad_sequence([torch.tensor(list(x[1].encode('utf-8')), dtype=torch.long) for x in results], batch_first=True, padding_value=0).to(accelerator.device)
    rating_tensor = torch.tensor([x[1] for x in results], dtype=torch.long).to(accelerator.device)

    # Gather results from all processes
    all_indices = accelerator.gather(indices_tensor)
    # all_texts = accelerator.gather(text_tensor)
    all_ratings = accelerator.gather(rating_tensor)

    # Only main process should sort and save results
    if accelerator.is_main_process:
        # Convert gathered tensors back to list of tuples
        gathered_results = {}

        indices_list = all_indices.cpu().tolist()
        ratings_list = all_ratings.cpu().tolist()

        # for idx, text, rating in zip(indices_list, all_texts, ratings_list):
        #     # gathered_results[idx] = (bytes(text[text != 0].cpu().numpy()).decode('utf-8'), rating)
        #     gathered_results[idx] = (bytes(text[text != 0].cpu().numpy()).decode('utf-8', errors='replace'), rating)

        # from concurrent.futures import ThreadPoolExecutor
        # def decode_text(text):
        #     return bytes(text[text != 0]).decode('utf-8', errors='replace')

        # with ThreadPoolExecutor() as executor:
        #     texts_decoded = list(executor.map(decode_text, [t.cpu().numpy() for t in all_texts]))
        # gathered_results = dict(zip(indices_list, zip(texts_decoded, ratings_list)))

        gathered_results = dict(zip(indices_list, zip(ratings_list)))

        # Sort results by original index
        sorted_results = sorted(gathered_results.items(), key=lambda x: x[0])
        # output_text_all = [x[1][0] for x in sorted_results]
        output_labels = [x[1][0] for x in sorted_results]

        # Save the merged results
        accelerator.end_training()

        output_dir = output_dir + f"{model_full_name}/{dataset_name}"
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        # final_text_path = f'{path}/output_text_all.pt'
        final_labels_path = f'{output_dir}/output_labels.pt'
        final_results_path = f'{output_dir}/results.pt'
        print(f"output_labels: {output_labels}")



        print("starting storing the outputs!!!")
        torch.save(sorted_results, final_results_path)
        # torch.save(output_text_all, final_text_path)
        torch.save(output_labels, final_labels_path)
        print('Finished generation and saving!')

        files_in_output_dir = os.listdir("/mnt/azureml/crunch/outputs/")
        print("Files in output directory:", files_in_output_dir)

        print("Main process is exiting...")





    

if __name__ == '__main__':
    fire.Fire(main)
    
    
    