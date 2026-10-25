<span id="c84133a4"></span>
# 附4：多轮reasoning_content的样本文件拆分
您可参照以下脚本，将一条携带多轮reasoning_content的样本拆分为多条仅最后一轮对话携带reasoning_content的样本。

* 代码实现：输入一个JSONL样例文件，输出处理好的训练数据到指定文件夹。
* 使用示例：python main.py \-\-input test.jsonl \-\-output res\-folder

```Python
import json
import os
import argparse
from process import process_sample
from typing import List, Dict, Any

def process_sample(sample: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    处理单个样例数据，根据规则生成一组处理后的样例
    
    参数:
        sample: 包含消息的字典，格式如案例所示
        
    返回:
        处理后的样例列表
    """
    messages = sample["messages"]
    
    # 用于存储处理后的消息
    message_result_list = []
    processed_messages = []
    not_set_loss_weight_index = []
    
    for i in range(0, len(messages)):
        current = messages[i]
        
        # 最后一个消息不需要处额外理
        if i == len(messages) - 1:
            processed_messages.append(current)
            message_result_list.append(processed_messages.copy())
            break
            
        # 只处理role为assistant的消息
        if current.get("role") == "assistant":
            # 检查是否存在loss_weight: 0
            has_loss_weight_zero = "loss_weight" in current and current["loss_weight"] == 0
            # 检查是否存在reasoning_content
            has_reasoning_content = "reasoning_content" in current
            
            # 情况1: 不存在loss_weight:0, 且存在reasoning_content
            if not has_loss_weight_zero and has_reasoning_content:
                # 拆分出当前消息, 并添加到消息列表中
                previous_messages = [msg.copy() for msg in processed_messages]
                previous_messages.append(current.copy())
                message_result_list.append(previous_messages.copy())
                
                # 处理当前消息, 不保留reasoning_content, 并记录loss_weight:0
                new_assistant_msg = current.copy()
                del new_assistant_msg["reasoning_content"]
                new_assistant_msg["loss_weight"] = 0
                
                # 将处理后的消息段保存下来，用于后续使用
                processed_messages.append(new_assistant_msg)
                current = []
                
                # 处理之前未设置loss_weight的消息，确保每条消息只被用于一次训练
                for idx in not_set_loss_weight_index:
                    processed_messages[idx]["loss_weight"] = 0
                not_set_loss_weight_index = []
                
            # 情况2: 存在loss_weight:0
            elif has_loss_weight_zero:
                # 不做拆分，不保留reasoning_content
                new_assistant_msg = current.copy()
                if "reasoning_content" in new_assistant_msg:
                    del new_assistant_msg["reasoning_content"]
                
                processed_messages.append(new_assistant_msg)
            # 情况3: 不存在reasoning_content
            else:
                processed_messages.append(current)
                # 记录下当前未设置loss_weight的消息
                not_set_loss_weight_index.append(i)
        else:
            # 非assistant角色的消息直接添加
            processed_messages.append(current)
    
    # 生成最终的样例列表
    processed_samples = []
    for messages in message_result_list:
        processed_sample = sample.copy()
        processed_sample["messages"] = messages
        processed_samples.append(processed_sample)
    
    return processed_samples

def main():
    parser = argparse.ArgumentParser(description='处理JSONL样例数据并输出到文件夹')
    parser.add_argument('--input', required=True, help='输入的JSONL文件路径')
    parser.add_argument('--output', required=True, help='输出文件夹路径')  
    
    args = parser.parse_args()
    
    # 读取JSONL文件
    samples = []
    try:
        with open(args.input, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():  # 跳过空行
                    samples.append(json.loads(line))
    except FileNotFoundError:
        print(f"错误: 找不到输入文件 {args.input}")
        return
    
    # 处理所有样例
    all_processed_samples = []
    for sample in samples:
        processed_samples = process_sample(sample)
        all_processed_samples.extend(processed_samples)
    
    # 写入JSONL文件到文件夹
    os.makedirs(args.output, exist_ok=True)
    
    try:
        # 确保输出目录存在
        os.makedirs(args.output, exist_ok=True)
        
        # 构建完整输出路径
        output_path = os.path.join(args.output, f"result.jsonl")
        
        # 将所有样本写入JSONL文件
        with open(output_path, 'w', encoding='utf-8') as f:
            for sample in all_processed_samples:
                f.write(json.dumps(sample, ensure_ascii=False) + '\n')
        
        print(f"处理完成，已生成JSONL文件，包含 {len(all_processed_samples)} 个样本，保存在 {output_path}")
    except Exception as e:
        print(f"错误: 写入文件失败 - {e}")

if __name__ == "__main__":
    main()
```