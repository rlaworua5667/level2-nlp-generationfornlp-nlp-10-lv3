# 표준 라이브러리
from ast import literal_eval
from copy import deepcopy

# 외부 라이브러리
import pandas as pd
import torch
from datasets import Dataset
from pymilvus import MilvusClient
from tqdm import tqdm
import nltk
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM


class BaseDataset(torch.utils.data.Dataset):
    def __init__(self, data,
                 tokenizer, configs, do_train = True, use_rag=False, rag_model=None):
        self.tokenizer = tokenizer
        self.max_length = configs.max_length
        self.configs = configs
        self.do_train = do_train
        self.use_rag = use_rag
        if self.use_rag:
            self.database = MilvusClient(uri=self.configs.database_path)
            self.rag_model = rag_model
            self.rewrite_prefix = self.configs.rewrite_prefix
            self.rewrite_model = AutoModelForSeq2SeqLM.from_pretrained(self.configs.rewrite_model)
            self.rewrite_tokenizer = AutoTokenizer.from_pretrained(self.configs.rewrite_model)
        self.dataset = self.format_data(data)
        self.tokens = self.tokenize(self.dataset)
        # if self.max_length is not None:
        #     self.tokens = self.tokens.filter(lambda x: len(x["input_ids"]) <= self.max_length)
        
    def __len__(self):
        return len(self.tokens)
    
    def __getitem__(self, idx):
        if self.do_train:
            return self.tokens[idx]
        else:
            return self.dataset[idx]

    def tokenize(self, dataset):
        def formatting_prompts_func(example):
            output_texts = []
            for i in range(len(example["messages"])):
                output_texts.append(
                    self.tokenizer.apply_chat_template(
                        example["messages"][i],
                        tokenize=False,
                    )
                )
            return output_texts

        def _tokenize(element):
            outputs = self.tokenizer(
                formatting_prompts_func(element),
                truncation=False,
                padding=False,
                return_overflowing_tokens=False,
                return_length=False,
            )
            return {
                "input_ids": outputs["input_ids"],
                "attention_mask": outputs["attention_mask"],
            }

        tokenized_dataset = dataset.map(
            _tokenize,
            remove_columns=list(dataset.features),
            batched=True,
            num_proc=1,
            load_from_cache_file=True,
            desc="Tokenizing",
        )

        return tokenized_dataset

    def format_data(self, dataset):
        def refactor_data(dataset) : # pandas Dataframe 
            records = []
            for _, row in dataset.iterrows():
                problems = literal_eval(row['problems'])
                record = {
                    'id': row['id'],
                    'paragraph': row['paragraph'],
                    'question': problems['question'],
                    'choices': problems['choices'],
                    'answer': problems.get('answer', None),
                    "question_plus": problems.get('question_plus', None),
                }
                # Include 'question_plus' if it exists
                if 'question_plus' in problems:
                    record['question_plus'] = problems['question_plus']
                records.append(record)
            
            df = pd.DataFrame(records)
            df['question_plus'] = df['question_plus'].fillna('')
            # df['full_question'] = df.apply(lambda x: x['question'] + ' ' + x['question_plus'] if x['question_plus'] else x['question'], axis=1)

            return df # pandas Dataframe 
        
        dataset = refactor_data(dataset)
        processed_dataset = []

        system_prompt = self.configs.PROMPT_SYSTEM_MESSAGE

        for i, row in dataset.iterrows():
            choices_string = "\n".join([f"{idx + 1} - {choice}" for idx, choice in enumerate(row["choices"])])
            if self.use_rag: # rag 사용 
                if len(row['paragraph']) < self.configs.rag_flag:
                    # top_k 문서 search
                    rewrite_result = self.rewrite_query(row)
                    search_texts = self.database.search(
                        collection_name=self.configs.collection_name,
                        data=[
                            self.rag_model.encode(rewrite_result) # row['query']
                        ],
                        limit=self.configs.top_k,
                        search_params={'metric_type': "COSINE", "params": {}},
                        output_fields=['text'], # Return the text field
                    )

                    # 지정한 k번째 문서들 이용 
                    hints = ""
                    for i, k in enumerate(self.configs.use_k):
                        hints += search_texts[0][k-1]['entity']['text']
                        if (len(self.configs.use_k) > 1) & (i < len(self.configs.use_k)-1):
                            hints += "\n"
                        
                    if row["question_plus"]: # <보기>가 있을 때
                        user_message = self.configs.RAG_PROMPT_QUESTION_PLUS.format(
                            paragraph=row["paragraph"],
                            question=row["question"],
                            question_plus=row["question_plus"],
                            choices=choices_string,
                            hint=hints,
                        )
                    
                    else: # <보기>가 없을 때
                        user_message = self.configs.RAG_PROMPT_NO_QUESTION_PLUS.format(
                            paragraph=row["paragraph"],
                            question=row["question"],
                            choices=choices_string,
                            hint=hints,
                        )
                else:
                    if row["question_plus"]: # <보기>가 있을 때
                        user_message = self.configs.PROMPT_QUESTION_PLUS.format(
                            paragraph=row["paragraph"],
                            question=row["question"],
                            question_plus=row["question_plus"],
                            choices=choices_string,
                        )
                    
                    else: # <보기>가 없을 때
                        user_message = self.configs.PROMPT_NO_QUESTION_PLUS.format(
                            paragraph=row["paragraph"],
                            question=row["question"],
                            choices=choices_string,
                        )
            else: # rag 사용 x 
                if row["question_plus"]: # <보기>가 있을 때
                    user_message = self.configs.PROMPT_QUESTION_PLUS.format(
                        paragraph=row["paragraph"],
                        question=row["question"],
                        question_plus=row["question_plus"],
                        choices=choices_string,
                    )
                
                else: # <보기>가 없을 때
                    user_message = self.configs.PROMPT_NO_QUESTION_PLUS.format(
                        paragraph=row["paragraph"],
                        question=row["question"],
                        choices=choices_string,
                    )
            
            # chat message 형식으로 변환
            if self.do_train: # 학습 및 검증 데이터
                processed_dataset.append(
                    {
                        "id": row["id"],
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_message},
                            {"role": "assistant", "content": f"{row['answer']}"}
                        ],
                        "label": row["answer"],
                    }
                )
            else: # 테스트 데이터 
                processed_dataset.append(
                    {
                        "id": row["id"],
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_message},
                        ],
                        "len_choices": len(row['choices']),
                    }
                )

        if self.rag_model:
            torch.cuda.empty_cache()
            del self.rag_model
        if self.rewrite_model:
            torch.cuda.empty_cache()
            del self.rewrite_model

        return Dataset.from_pandas(pd.DataFrame(processed_dataset)) 

    def rewrite_query(self, row):
        rewrite_sample = f"지문: {row['paragraph']}\n질문: {row['question']}\n선지: {row['choices']}"
        rewrite_inputs = [self.rewrite_prefix+rewrite_sample]
        rewrite_inputs = self.rewrite_tokenizer(rewrite_inputs, max_length=1024, truncation=True, return_tensors='pt')
        rewrite_output = self.rewrite_model.generate(
            **rewrite_inputs,
            do_sample=True,
            min_length=10,
            max_length=64
        )
        rewrite_decoded_output = self.rewrite_tokenizer.batch_decode(rewrite_output, skip_special_tokens=True)[0]
        rewrite_result = nltk.sent_tokenize(rewrite_decoded_output.strip())[0]
        
        return rewrite_result

class FineTuningDataset(torch.utils.data.Dataset):
    def __init__(self, data, tokenizer, configs, is_eval=False):
        self.tokenizer = tokenizer
        self.configs = configs
        self.is_eval = is_eval

        # 데이터 전처리를 한 번만 수행
        self.tokenized_data = data.map(self.preprocess_function, batched=True)

    def __len__(self):
        return len(self.tokenized_data)

    def __getitem__(self, idx):
        return self.tokenized_data[idx]

    def preprocess_function(self, examples):
        if self.is_eval:
            # 평가 데이터 전처리
            prompt = "\n질문에 알맞은 선택지를 골라 정답만 출력하세요. \n정답:"
            inputs = self.tokenizer(
                ["질문: "+t+"\n선택지: "+c+prompt for t, c in zip(examples["question"], examples['choice'])], # 
                truncation=True,
                padding="max_length",
                max_length=self.configs.max_length,
            )
            # 타겟 데이터 전처리
            with self.tokenizer.as_target_tokenizer():
                labels = self.tokenizer(
                    examples["answer"],
                    truncation=True,
                    padding="max_length",
                    max_length=self.configs.max_length,
                )

            # 패딩 토큰을 -100으로 변경
            labels["input_ids"] = [
                [-100 if token == self.tokenizer.pad_token_id else token for token in label]
                for label in labels["input_ids"]
            ]
            inputs["labels"] = labels["input_ids"]

        else:
            # 학습 데이터 전처리
            inputs = self.tokenizer(
                examples["text"],
                truncation=True,
                padding="max_length",
                max_length=self.configs.max_length,
            )

            # labels 생성 및 한 칸씩 이동
            inputs["labels"] = deepcopy(inputs["input_ids"])
            for i in range(len(inputs["labels"])):
                inputs["labels"][i] = inputs["labels"][i][1:] + [self.tokenizer.pad_token_id]

            # 패딩 토큰을 -100으로 변경
            inputs["labels"] = [
                [-100 if token == self.tokenizer.pad_token_id else token for token in label]
                for label in inputs["labels"]
            ]

        return inputs
    
class ReasoningDataset(torch.utils.data.Dataset):
    def __init__(
            self,
            data,
            tokenizer,
            configs,
            do_train=True,
    ):
        self.data = data
        self.tokenizer = tokenizer
        self.do_train = do_train
        self.USER_PROMPT_TEMPLATE = configs.USER_PROMPT

    def __len__(self):
        return self.data.shape[0]

    def __getitem__(self, idx):
        paragraph = self.data.loc[idx, "paragraph"]
        question = self.data.loc[idx, "question"]
        choices = self.data.loc[idx, "choices"]
        reason = self.data.loc[idx, "reason"]
        
        SYSTEM_PROMPT = self.data.loc[idx, "reason"]
        USER_PROMPT = self.USER_PROMPT_TEMPLATE.format(
            paragraph=paragraph,
            question=question,
            choices=choices,
        )

        message = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT},
            {"role": "assistant", "content": reason}
        ]

        tokenized = self.tokenizer.apply_chat_template(
            message,
            tokenize=False,
        )

        inputs = self.tokenizer(
            tokenized,
            truncation=False,
            padding=False,
            return_overflowing_tokens=False,
            return_length=False,
            # add_special_token=False,
            return_attention_mask=True,
        )

        if self.do_train:
            return {
                "input_ids": inputs["input_ids"],
                "attention_mask": inputs["attention_mask"],
                "labels": inputs["input_ids"].copy(),
            }
        return {
            "input_ids": inputs["input_ids"],
            "attention_mask": inputs["attention_mask"],
        }