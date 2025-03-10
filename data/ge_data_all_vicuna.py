import argparse
from tqdm import tqdm
from PIL import Image
from transformers import AutoProcessor, LlavaForConditionalGeneration

parser = argparse.ArgumentParser(description='sp')
parser.add_argument('--start', type=int, default=0)
parser.add_argument('--end', type=int, default=100)
parser.add_argument('--index', type=int, default=1)
parser.add_argument('--gpu_index', type=int, nargs='+', default=[0])
parser.add_argument('--outdir', type=str, default='outdir0')
args = parser.parse_args()
import os
print("!!!!!!!!!!!!!", str(args.gpu_index)[1:-1])
os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu_index)[1:-1]

import torch
import torch.nn.functional as F
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer
from datasets import load_dataset
from fastchat.model.model_adapter import get_conversation_template

bigname="mistral-community/pixtral-12b"

def longest_common_prefix(list1, list2):
    prefix_length = 0
    min_length = min(len(list1), len(list2))

    for i in range(min_length):
        if list1[i] == list2[i]:
            prefix_length += 1
        else:
            break

    common_prefix = list1[:prefix_length]
    return common_prefix, prefix_length


def build_dataset_rank(
        tokenizer, split="train",
        select=None,
        data_path = "/cache/CKPT/ShareGPT_V4.3_unfiltered_cleaned_split.json"
):
    #ds = load_dataset('json', data_files=data_path)
    
    ds = load_dataset("Areen007/pixtral_data")
    ds = ds['train']
    print(ds)
    ds = ds.shuffle(seed=42)
    ds1 = ds.select(range(args.start, args.end))
    original_columns1 = ds1.column_names
    num_proc = 4

    # need to be modify for summarization
    def preprocess_function(examples):
        new_examples = {
            "conversation":[],
            "input_ids": [],
            "pixel_values": [],
            "image_sizes": [],
            "loss_mask": []
        }
        for i in range(len(examples)):
            # conv = get_conversation_template("vicuna")
            # roles = {"human": conv.roles[0], "gpt": conv.roles[1]}
            # source= examples['conversations'][i]
            # if roles[source[0]["from"]] != conv.roles[0]:
            #     # Skip the first one if it is not from human
            #     source = source[1:]
            # conv.messages = []
            # for j, sentence in enumerate(source):
            #     role = roles[sentence["from"]]
            #     assert role == conv.roles[j % 2], f"{i}"
            #     conv.append_message(role, sentence["value"])
            # conversation=conv.get_prompt()
            image = examples['image'][i]
            caption = examples['caption'][i]
            conversation = [
                {
                    "role": "user",
                    "content": [
                        {"type": "image"},
                        {"type": "text", "content": "What is shown in this image?"},
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "content": caption},
                    ],
                }
            ]
            prompt = tokenizer.apply_chat_template(
                conversation,
            )

            inputs = tokenizer(text=prompt, images=[image], return_tensors="pt")
            input_ids = inputs['input_ids']
            pixel_values = inputs['pixel_values']
            image_size = inputs['image_sizes']

            loss_mask=torch.ones_like(input_ids[0])

            loss_mask[:4170] = 0
            
            # turns = conversation.split(conv.sep2)
            # cur_len = 1
            # loss_mask[:cur_len] = 0
            # for i, turn in enumerate(turns):
            #     if turn == "":
            #         break
            #     turn_len = len(tokenizer(turn).input_ids)

            #     parts = turn.split(sep)
            #     if len(parts) != 2:
            #         break
            #     parts[0] += sep
            #     # "-2" is hardcoded for the Llama tokenizer to make the offset correct.
            #     instruction_len = len(tokenizer(parts[0]).input_ids) - 2

            #     if i != 0 and not tokenizer.legacy:
            #         # The legacy and non-legacy modes handle special tokens differently
            #         instruction_len -= 1

            #     # Ignore the user instructions
            #     loss_mask[cur_len: cur_len + instruction_len] = 0
            #     cur_len += turn_len

            #     if i != 0 and not tokenizer.legacy:
            #         # The legacy and non-legacy modes handle special tokens differently
            #         cur_len -= 1

            # loss_mask[cur_len:] = 0

            new_examples["conversation"].append(conversation)
            new_examples["input_ids"].append(input_ids)
            new_examples["loss_mask"].append(loss_mask[None,:])
            new_examples["pixel_values"].append(pixel_values)
            new_examples["image_sizes"].append(image_size)


        return new_examples

    ds1 = ds1.map(
        preprocess_function,
        batched=True,
        num_proc=1,
        remove_columns=original_columns1,
        load_from_cache_file=False
    )

    ds1.set_format(type="torch")
    print(ds1)
    return ds1

bigtokenizer = AutoProcessor.from_pretrained(bigname, use_fast=False)
ds = build_dataset_rank(bigtokenizer)
print(ds)
bigmodel = LlavaForConditionalGeneration.from_pretrained(bigname,  device_map="auto", torch_dtype=torch.float16)
bigmodel.eval()



@torch.no_grad()
def ge(data):
    input_ids=data["input_ids"].to(bigmodel.device, dtype=torch.long)
    pixel_values = data['pixel_values'].to(bigmodel.device, dtype=torch.float16)
    image_sizes = data['image_sizes'].to(bigmodel.device, dtype=torch.int32)
    print(pixel_values.shape)
    outs_big = bigmodel.generate(input_ids = input_ids, 
                        pixel_values=pixel_values, 
                        image_sizes = image_sizes,
                        max_new_tokens=36,
                        output_hidden_states=True)
    print(len(outs_big.hidden_states))
    #assert len(outs_big.hidden_states) == 33

    max_prob_tokens_big = torch.argmax(outs_big.logits, dim=-1)
    probs = torch.softmax(outs_big.logits, dim=-1)
    maxp =probs[0].max(dim=1).values
    td={"input_ids":input_ids.cpu()[0],
        "loss_mask":data["loss_mask"].cpu()[0]}
    # early exit layer 
    # exit at layer2 for vicuna-7B and layer3 for vicuna-13B 
    td[f"hidden_state_layer2"] = outs_big.hidden_states[2].cpu()[0]
    td[f"hidden_state_layer3"] = outs_big.hidden_states[3].cpu()[0]
    td[f"hidden_state"] = outs_big.hidden_states[-1].cpu()[0]
    return td

outdir = f'{args.outdir}/{args.index}'
if not os.path.exists(outdir):
    os.makedirs(outdir)

def writedata(name,data_point):
    if not os.path.exists(name):
        os.makedirs(name)
    current_length=len(os.listdir(name))
    idx=current_length
    torch.save(data_point, f'{name}/data_{idx}.ckpt')


for data in tqdm(ds):
    outdata = ge(data)
    writedata(outdir,outdata)


