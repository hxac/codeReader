# 构建聊天界面（Chainlit）

## 1. 本讲目标

前 25 讲里，我们与模型交互的方式始终是「写脚本 → 跑脚本 → 看控制台输出」。本讲把 from-scratch 模型包装成一个真正可交互的 Web 聊天应用：浏览器里打开页面、打字、看着模型一个 token 一个 token 地流式吐字。

学完本讲，你应该能够：

1. 理解 Chainlit 的事件驱动编程模型：`@chainlit.on_chat_start` 与 `@chainlit.on_message` 两个回调如何支撑一个聊天应用。
2. 读懂 `chG` 目录下的单轮与多轮两个界面脚本，说清它们各自的消息处理流水线。
3. 掌握多轮对话历史的表示、手工重建 Qwen 聊天模板的方法。
4. 推导 `trim_input_tensor` 的上下文裁剪公式，理解「prompt 长度 + 生成预算 ≤ 上下文窗口」这条硬约束背后的数学，以及裁错会发生什么。
5. 会用 `tests/test_chat_trimming.py` 验证裁剪逻辑——包括它用 `ast` 只提取辅助函数、避免加载整个模型的花招。

## 2. 前置知识

- **Chainlit**：一个开源的 Python 包，专门用来给 LLM 应用快速搭聊天界面（类似为 LLM 定制版的 Streamlit）。你只写两个异步回调函数，它负责 HTTP 服务器、WebSocket 推送、前端页面渲染。安装与运行方式见其[文档](https://github.com/Chainlit/chainlit)。
- **回调（callback）与事件驱动**：你不写 `while True` 主循环，而是注册「某事件发生时请调用我」的函数。Chainlit 的两个核心事件：新会话开始（`on_chat_start`）、用户发来一条消息（`on_message`）。
- **异步函数与 `await`**：Python 的 `async def` 函数里，`await xxx()` 表示「等待一个耗时操作完成（如网络推送 token 给浏览器），期间不阻塞服务器处理其他会话」。本讲不需要你深入 asyncio，只要能读懂 `await out_msg.stream_token(piece)` 是「把一个 token 推到浏览器」即可。
- **流式生成器**：u3-l2 讲过 `generate_text_basic_stream_cache` 是一个 Python generator，`for tok in ...` 每次取回一个新 token（形状 `(1, 1)` 的张量），内部走 prefill + KV 缓存 decode。本讲的界面就是它的一个「消费者」。
- **聊天模板**：u2-l6 讲过 Qwen 的聊天格式——每条消息被包成 `<|im_start|>角色\n内容<|im_end|>\n`，最后追加 `<|im_start|>assistant\n` 引导模型接话。本讲的多轮脚本要**手工**拼出这段格式。
- **上下文窗口与位置游标**：u2-l1 讲过 `QWEN_CONFIG_06_B` 的 `context_length=40960`；u2-l5 讲过 `current_pos` 超过 `context_length` 时模型会抛 `ValueError`。这两个事实就是本讲裁剪逻辑的全部动机。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [chG/01_main-chapter-code/qwen3_chat_interface.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py) | 单轮聊天界面：每条消息独立编码，不记忆历史 |
| [chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py) | 多轮聊天界面：维护对话历史并做上下文裁剪，本讲主角 |
| [chG/01_main-chapter-code/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md) | 附录 G 说明：Python 3.13 要求、pip/uv 安装、`chainlit run` 启动、`CHECKPOINT_PATH` 用法 |
| [tests/test_chat_trimming.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py) | 裁剪函数的参数化测试，同时覆盖 CLI 版与 Chainlit 版 |
| [ch02/05_use_model/chat_multiturn.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py) | 第 2 章的终端版多轮 REPL，是 Chainlit 多轮版的原型，测试也覆盖它 |
| [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) | `Qwen3Tokenizer.encode` / `_wrap_chat`：理解模板包装行为的钥匙 |
| [reasoning_from_scratch/ch03.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py) | `load_model_and_tokenizer` / `load_tokenizer_only`：界面脚本复用的加载入口 |
| [reasoning_from_scratch/ch02.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py) | `generate_text_basic_stream_cache`：界面消费的流式生成器 |
| [pyproject.toml](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml) | `extra` 可选依赖组声明 `chainlit>=2.8.3`（带 Python 版本标记） |

两个 Chainlit 脚本结构几乎完全相同（配置区、加载函数、双回调），差别只在：单轮版的 `main` 只编码当前消息；多轮版的 `main` 多了「取历史 → 拼模板 → 裁剪 → 写回历史」四步。建议对照阅读。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**4.1 应用骨架与模型加载时机**、**4.2 单轮流式回复流水线**、**4.3 多轮历史与手写聊天模板**、**4.4 上下文裁剪的预算数学**。

### 4.1 Chainlit 应用骨架：回调驱动与模型加载时机

#### 4.1.1 概念说明

一个 Chainlit 应用就是一个普通 Python 文件，Chainlit 服务器启动时把它当模块导入执行，然后等待事件。本项目的脚本把代码分成三层：

1. **配置区**：文件顶部几个常量（`WHICH_MODEL`、`MAX_NEW_TOKENS`、`CHECKPOINT_PATH`、`COMPILE`），想换模型就改这里。
2. **模块级模型加载**：`MODEL, TOKENIZER = load_app_model_and_tokenizer()` 写在模块顶层。它在**服务启动时执行一次**（此时还没有任何用户连接），此后所有会话、所有消息共享同一份权重——0.6B 模型加载一次就够了，绝不能放进每条消息都触发的回调里。
3. **事件回调**：`on_chat_start` 为**每个新会话**初始化一份独立的 `history`（通过 `chainlit.user_session`，Chainlit 的会话级键值存储）；`on_message` 处理每条用户消息。

有两个工程细节值得注意：

- **为什么用环境变量传 checkpoint 而不用 argparse？** 因为 `chainlit run app.py` 命令行已经被 Chainlit 自己占用了，你的脚本拿不到额外命令行参数，所以改用 `CHECKPOINT_PATH` 环境变量（README 也专门解释了这一点）。
- **为什么 EOS 有两个？** reasoning 模型的官方 eos 是 `<|im_end|>`，base 模型是 `<|endoftext|>`，而自定义 checkpoint（比如第 7/8 章训出来的）可能吐出其中任何一个，所以干脆两个都盯。

#### 4.1.2 核心流程

```text
chainlit run qwen3_chat_interface.py
  └─ Chainlit 服务器导入本文件（模块级代码执行）
       ├─ DEVICE = get_device()                 # 设备探测（u3-l1）
       ├─ load_app_model_and_tokenizer()        # 下载（如需）+ 加载权重，仅此一次
       │    ├─ CHECKPOINT_PATH 未设置 → load_model_and_tokenizer(...)  # 默认权重
       │    └─ CHECKPOINT_PATH 已设置 → 只下载分词器 + torch.load 灌权重
       └─ EOS_TOKEN_IDS = (<|im_end|>, <|endoftext|>) 的 token id
  └─ 浏览器打开新会话 → 触发 on_chat_start → 初始化该会话的 history
  └─ 用户每发一条消息 → 触发 main(message) → 生成并流式返回
```

#### 4.1.3 源码精读

配置区：`WHICH_MODEL` 决定加载 base 还是 reasoning；`MAX_NEW_TOKENS=38912` 这个奇怪的数字不是随手写的，4.4 节会揭示它与 `context_length=40960` 的精确配平关系；`CHECKPOINT_PATH` 从环境变量读取，用于加载第 7 章 GRPO 或第 8 章蒸馏的自定义 checkpoint：

[Qwen3 chat interface.py:L21-L35](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L21-L35) —— 配置块：`WHICH_MODEL`/`MAX_NEW_TOKENS`/`LOCAL_DIR`/`CHECKPOINT_PATH`/`COMPILE` 五个常量，注释里给出了带 `CHECKPOINT_PATH` 的完整启动命令示例。

加载函数有两条互斥分支。默认分支直接复用第 3 章的统一加载入口（base 走裸文本分词器、reasoning 走带聊天模板的分词器，见 u4-l1）；checkpoint 分支则只下载分词器（`load_tokenizer_only`），手工新建 `Qwen3Model` 并用 `torch.load` + `load_state_dict` 灌权重——这正是 u8-l5/u9-l5 反复强调的「checkpoint 就是纯 `state_dict`，必须先新建同构模型再灌入」的复用模式：

[qwen3_chat_interface.py:L41-L63](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L41-L63) —— `load_app_model_and_tokenizer`：无 `CHECKPOINT_PATH` 时走 `load_model_and_tokenizer`；有则检查文件存在性、`load_tokenizer_only` 取分词器、`Qwen3Model(QWEN_CONFIG_06_B)` 建空模型后 `load_state_dict` 灌权重并搬上设备，可选 `torch.compile`。

[qwen3_chat_interface.py:L66](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L66) —— 模块级赋值 `MODEL, TOKENIZER = load_app_model_and_tokenizer()`：服务启动时执行一次，全进程共享。

[qwen3_chat_interface.py:L68-L73](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L68-L73) —— `EOS_TOKEN_IDS` 元组：对纯特殊 token 调 `encode` 时，`Qwen3Tokenizer.encode` 走「单个特殊 token 直接查表」的捷径（见 [qwen3.py:L393-L395](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L393-L395)），`[0]` 即取该 token 的 id。

[qwen3_chat_interface.py:L76-L81](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L76-L81) —— `@chainlit.on_chat_start` 回调：为当前会话创建空 `history` 并塞入一条 system 消息。注意单轮版的 `main` 其实**并没有使用**这份 history——它是多轮版共享的骨架代码，留着作为升级的接口。

顺带一提，两个界面脚本复用的加载入口在 [ch03.py:L52-L90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L52-L90)（`load_model_and_tokenizer`，reasoning 分支给分词器开 `apply_chat_template=True, add_generation_prompt=True, add_thinking=True`）与 [ch03.py:L93-L118](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L93-L118)（`load_tokenizer_only`，`tokenizer_only=True` 只拉分词器文件）。

#### 4.1.4 代码实践

**实践目标**：不启动界面，先做一次「dry run」——让 `CHECKPOINT_PATH` 指向一个不存在的文件，验证加载函数的防御分支。这一步只需要 torch 和已下载的分词器（约几 MB），不需要模型权重。

1. 操作步骤：
   - 在仓库根目录建一个临时脚本（示例代码，放在 `qwen3/` 之外的任意目录）：

     ```python
     # 示例代码：verify_loader_branch.py
     import os
     from pathlib import Path
     os.environ["CHECKPOINT_PATH"] = "/tmp/does-not-exist.pth"
     import importlib.util
     # 只提取函数，避免触发模块级加载整个界面（技巧同 test_chat_trimming.py）
     src = Path("chG/01_main-chapter-code/qwen3_chat_interface.py").read_text()
     import ast
     tree = ast.parse(src)
     fn = next(n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "load_app_model_and_tokenizer")
     ns = {}
     exec(compile(ast.Module(body=[fn], type_ignores=[]), "<fn>", "exec"), ns)
     try:
         ns["load_app_model_and_tokenizer"]()
     except FileNotFoundError as e:
         print("捕获预期异常:", e)
     ```

   - 运行 `python verify_loader_branch.py`（需先按 README 完成依赖安装）。
2. 需要观察的现象：终端打印 `捕获预期异常: Checkpoint file not found: /tmp/does-not-exist.pth`。
3. 预期结果：说明 checkpoint 分支在碰任何下载/加载之前先做了存在性检查，报错信息直接指出坏路径。
4. 若本机尚未配置好 Python 3.13 环境，此实践标注「待本地验证」；逻辑本身可直接从 [L50-L52](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L50-L52) 的代码读出。

#### 4.1.5 小练习与答案

**练习 1**：为什么界面脚本用环境变量而不是 argparse 传 checkpoint 路径？
**答案**：`chainlit run app.py` 会把命令行参数留给 Chainlit 自己，脚本无法再解析自定义参数；环境变量是进程启动前注入的旁路通道，README 的「Using a custom checkpoint」一节明确说明了这一取舍。

**练习 2**：把 `MODEL, TOKENIZER = load_app_model_and_tokenizer()` 移到 `main` 回调内部会发生什么？
**答案**：每一条用户消息都会重新走一遍「下载检查 + 建模型 + `load_state_dict` + 搬设备」，0.6B 权重的加载耗时（GPU 上数秒、CPU 上更长）会叠加到每次回复延迟上；模块级加载保证「一次加载、全会话共享」。

**练习 3**：`EOS_TOKEN_IDS` 为什么要同时含 `<|im_end|>` 和 `<|endoftext|>`？
**答案**：两个脚本默认 `WHICH_MODEL = "reasoning"`（官方 eos 是 `<|im_end|>`），但通过 `CHECKPOINT_PATH` 可以挂载 base 权重或自定义 GRPO/蒸馏 checkpoint，它们可能吐 `<|endoftext|>`；[L68-L69](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L68-L69) 的注释写明「some custom checkpoints emit both」，盯住两个 token 才不会出现模型说完话界面还在傻等的情况。

### 4.2 单轮流式回复：`main` 回调的四步流水线

#### 4.2.1 概念说明

单轮版的 `main` 是一个教科书式的流式回复流水线：**编码 → 建流式消息 → 逐 token 推送 → 定稿**。核心是 Chainlit 的 `Message` 对象三段式用法：

- `out_msg = chainlit.Message(content="")` + `await out_msg.send()`：先在聊天窗口发出一条**空消息**，拿到一个可以持续更新的句柄；
- `await out_msg.stream_token(piece)`：把一个文本片段增量追加到这条消息上，浏览器实时刷新——这就是「打字机效果」的全部秘密；
- `await out_msg.update()`：生成结束后定稿（后续若要改写消息也走它）。

与之配套的是 u3-l2 的流式生成器：`for tok in generate_text_basic_stream_cache(...)` 每循环一次拿回一个新 token。注意该生成器是**贪心解码**（`argmax`，见 [ch02.py:L117-L130](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L117-L130)），所以界面输出是确定性的，没有温度采样。

还有一个分工细节：生成器本身支持 `eos_token_id` 参数，但界面**没有**传（多轮版里这行还被注释掉了），EOS 判断被提升到了应用层——因为应用层要同时盯两个 EOS token（4.1 已述）。

#### 4.2.2 核心流程

```text
main(message):
  1) input_ids = TOKENIZER.encode(message.content)     # reasoning 分词器自动套聊天模板
     input_ids_tensor = tensor(...).unsqueeze(0)       # (1, T)
  2) out_msg = chainlit.Message(""); await out_msg.send()   # 空消息占位
  3) for tok in generate_text_basic_stream_cache(           # u3-l2 的生成器
         model=MODEL, token_ids=input_ids_tensor,
         max_new_tokens=MAX_NEW_TOKENS):
        token_id = tok.squeeze(0).item()               # (1,1) → 标量
        if token_id in EOS_TOKEN_IDS: break            # EOS 不推送、直接收尾
        piece = TOKENIZER.decode([token_id])           # 单 token 解码
        await out_msg.stream_token(piece)              # 推给浏览器
  4) await out_msg.update()                            # 定稿
```

其中第 1 步对 reasoning 分词器并不只是「查表」：`encode` 默认（`chat_wrapped=None`）回落到 `apply_chat_template=True`，会调用 `_wrap_chat` 把裸文本包成 `<|im_start|>user\n...<|im_end|>\n<|im_start|>assistant\n`（见 [qwen3.py:L389-L398](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L389-L398) 与 [qwen3.py:L411-L419](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L411-L419)），`add_thinking=True` 只补一个换行、让模型自己写 `<think>` 标签（u2-l6）。

#### 4.2.3 源码精读

[qwen3_chat_interface.py:L89-L91](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L89-L91) —— 第 1 步：把用户消息编码为 token id 列表并升维成 `(1, T)` 批量张量。

[qwen3_chat_interface.py:L93-L95](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L93-L95) —— 第 2 步：发出空消息，拿到流式句柄。

[qwen3_chat_interface.py:L97-L107](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L97-L107) —— 第 3 步：消费生成器。`tok.squeeze(0).item()` 把 `(1,1)` 张量化成 Python 整数；EOS 检查放在 `stream_token` **之前**，保证结束符本身不显示；`decode([token_id])` 逐 token 解码（`Qwen3Tokenizer.decode` 保留特殊 token，见 [qwen3.py:L408-L409](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L408-L409)，因此 `<think>` 之类的标签会原样出现在界面上）。

[qwen3_chat_interface.py:L109-L110](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L109-L110) —— 第 4 步：`await out_msg.update()` 定稿消息。

#### 4.2.4 代码实践

**实践目标**：把「逐 token 推送」与「EOS 提前停止」拆开观察——不启动 Web 界面，直接在终端模拟 `main` 的第 3、4 步。

1. 操作步骤（示例代码，可在任意已装好项目依赖的环境运行；权重需已下载到 `qwen3/`）：

   ```python
   # 示例代码：simulate_stream.py
   import torch
   from reasoning_from_scratch.ch02 import generate_text_basic_stream_cache, get_device
   from reasoning_from_scratch.ch03 import load_model_and_tokenizer

   device = get_device()
   model, tok = load_model_and_tokenizer("reasoning", device=device, use_compile=False)
   eos_ids = (tok.encode("<|im_end|>")[0], tok.encode("<|endoftext|>")[0])

   ids = tok.encode("What is 2+2?")            # 自动套聊天模板
   t = torch.tensor(ids, device=device).unsqueeze(0)
   pieces = []
   for tk in generate_text_basic_stream_cache(model=model, token_ids=t, max_new_tokens=512):
       tid = tk.squeeze(0).item()
       if tid in eos_ids:
           print("\n[EOS 达到，停止]"); break
       piece = tok.decode([tid])
       pieces.append(piece); print(piece, end="", flush=True)   # 打字机效果
   ```

   - `python simulate_stream.py` 运行。
2. 需要观察的现象：字符逐段（而非一次性）出现在终端；thinking 模型会先输出 `<think>` 段再给答案；到达 eos 前循环提前退出。
3. 预期结果：你看到的「逐段打印」与浏览器里 `stream_token` 的打字机效果是同一机制，只是终点从 WebSocket 换成了 stdout。
4. CPU 上 0.6B 模型可用但较慢；具体输出文本因模型版本而异，属「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么逐 token `decode([token_id])` 而不是生成完一次性 decode？
**答案**：逐 token 解码 + `stream_token` 才能实现流式体验，用户首 token 即可见；一次性 decode 要等全部生成结束。代价是 BPE 分词在极少数边界上可能合并 token，逐 token 解码偶尔与整段解码有细微差异——界面场景下这个代价可接受。

**练习 2**：`send()` 和 `stream_token()` 的分工是什么？
**答案**：`send()` 把（此时为空的）消息对象注册到聊天窗口、返回可更新句柄；`stream_token()` 在这条已有消息上增量追加文本。前者只发生一次，后者每个 token 一次。

**练习 3**：如果把 EOS 检查移到 `stream_token` 之后，界面会怎样？
**答案**：结束符本身（如 `<|im_end|>`，decode 保留特殊 token）会被当成正文显示在气泡末尾；先检查后推送的现有顺序把它拦在了显示之外。

### 4.3 多轮变体：历史管理与手写聊天模板

#### 4.3.1 概念说明

单轮版每条消息都是「失忆」的；多轮版让模型记住上文。做法朴素而通用：**把整段对话历史重新编码成一个 prompt**，每轮重建、每轮全量前向。KV 缓存只在单次生成的函数内部存活（u3-l2），跨轮不复用——牺牲一点效率换来实现极简。

历史用「role/content 字典列表」表示，存在 `chainlit.user_session` 里，每个浏览器会话一份：

```python
[
  {"role": "system",    "content": "You are a helpful assistant."},
  {"role": "user",      "content": "第一问"},
  {"role": "assistant", "content": "第一答"},
  {"role": "user",      "content": "第二问"},
]
```

`build_prompt_from_history` 把它手工渲染成 Qwen 聊天格式：每条消息包一层 `<|im_start|>角色\n内容<|im_end|>\n`，最后追加 `<|im_start|>assistant\n` 作为「轮到助手说话」的发令枪。

一个值得认真对待的细节：**模板包装可能发生两层**。reasoning 分词器的 `encode` 在 `chat_wrapped=None` 时回落到 `apply_chat_template=True`（[qwen3.py:L389-L398](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L389-L398)），而多轮 Chainlit 脚本喂给 `encode` 的已经是手工包好模板的文本——从源码逻辑推断，这层文本会被再包进一个 `user` 块里。对比第 2 章的 CLI 原型 `chat_multiturn.py`，它显式用了 `apply_chat_template=False` 并注释「We will apply the chat template manually later」（[chat_multiturn.py:L65-L69](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py#L65-L69)），才是「手写模板 + 裸编码」的严格组合。两个脚本在这一点上不一致；对 0.6B 模型的实际回复质量影响多大，建议按 4.3.4 的实践打印 token 序列亲自验证（待本地验证）。

#### 4.3.2 核心流程

```text
on_chat_start: history = [system]

main(message):
  0) history.append({"role": "user", "content": message.content})
  1) prompt = build_prompt_from_history(history, add_assistant_header=True)
     input_ids = TOKENIZER.encode(prompt)                  # 注意 4.3.1 的双重包装问题
     input_ids_tensor = tensor(...).unsqueeze(0)
     input_ids_tensor = trim_input_tensor(                 # ← 4.4 节主角
         input_ids_tensor,
         context_len=MODEL.cfg["context_length"],          # 40960
         max_new_tokens=MAX_NEW_TOKENS)                    # 38912
  2) out_msg 发空消息
  3) 流式生成（与单轮版相同；EOS 双 token 监控）
  4) out_msg.update()
  5) history.append({"role": "assistant", "content": out_msg.content})
     user_session.set("history", history)                  # 写回，供下一轮拼接
```

与第 2 章 CLI 版（[chat_multiturn.py:L136-L184](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py#L136-L184) 的 `run_generate`）逐步同构，只是终点从 `print(piece, end="")` 换成 `await out_msg.stream_token(piece)`、历史从模块级全局变量换成会话级 `user_session`，并多了 `\clear`/`\history` 两个 REPL 命令（[L196-L215](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py#L196-L215)）。

#### 4.3.3 源码精读

[qwen3_chat_interface_multiturn.py:L51-L63](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L51-L63) —— `build_prompt_from_history`：循环拼接 `<|im_start|>{role}\n{content}<|im_end|>\n`，`add_assistant_header=True` 时末尾追加 `<|im_start|>assistant\n`。模板是手写字符串，不经过分词器的模板开关。

[qwen3_chat_interface_multiturn.py:L104-L109](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L104-L109) —— `on_start`：为会话播种 system 消息，这是每轮 prompt 的第一块。

[qwen3_chat_interface_multiturn.py:L117-L131](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L117-L131) —— 第 0、1 步：用户消息入史 → 手工拼模板 → 编码 → 裁剪。注释「Multi-turn can be very long, so we add this left-trimming」点明裁剪动机：多轮历史会无限增长，而窗口是硬的。

[qwen3_chat_interface_multiturn.py:L137-L148](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L137-L148) —— 流式生成循环：与单轮版相同，只是 `token_id` 保持为张量、用 `tolist()` 解码；`eos_token_id` 参数那行被注释掉，EOS 判断在应用层做。

[qwen3_chat_interface_multiturn.py:L150-L155](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L150-L155) —— 第 5 步：把助手回复（`out_msg.content`，即流式累计的全文）写回历史。「每轮结束写回」是多轮记忆的唯一持久化动作——`user_session.set` 确保下一个回调取到的是更新后的列表。

#### 4.3.4 代码实践

**实践目标**：不上模型，肉眼检查「历史 → prompt 文本」的映射，并顺带验证 4.3.1 提到的双重包装问题。

1. 操作步骤（示例代码，零 GPU、零权重，几秒跑完）：

   ```python
   # 示例代码：inspect_history_prompt.py
   # build_prompt_from_history 是纯函数，直接复制自 multiturn 脚本 L51-L63
   def build_prompt_from_history(history, add_assistant_header=True):
       parts = []
       for m in history:
           parts.append(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n")
       if add_assistant_header:
           parts.append("<|im_start|>assistant\n")
       return "".join(parts)

   history = [
       {"role": "system", "content": "You are a helpful assistant."},
       {"role": "user", "content": "我叫小明"},
       {"role": "assistant", "content": "你好，小明！"},
       {"role": "user", "content": "我叫什么？"},
   ]
   print(build_prompt_from_history(history))
   ```

   - 运行并对照输出，标出每条消息的 `<|im_start|>...<|im_end|>` 边界与末尾的 assistant 头。
   - 进阶（可选）：若本地已装依赖，加载 reasoning 分词器后分别执行 `tok.encode(prompt)` 与 `tok.encode(prompt, chat_wrapped=False)`，比较两个 id 序列长度与开头若干 id，验证「默认路径会再包一层」的推断。
2. 需要观察的现象：输出是四段消息块加一个 assistant 头；进阶部分两个序列长度不同（默认版更长）即证实双重包装。
3. 预期结果：纯函数部分输出确定；进阶部分的具体差异「待本地验证」。
4. 说明：CLI 版用 `chat_wrapped=False` 等价手段（初始化即关闭模板）避免了这一层，见 [chat_multiturn.py:L65-L69](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py#L65-L69)。

#### 4.3.5 小练习与答案

**练习 1**：为什么每轮都把全部历史重新编码、重新 prefill，而不是复用上一轮的 KV 缓存？
**答案**：本仓库的 KV 缓存生命周期被封在 `generate_text_basic_stream_cache` 函数内部（每次调用新建、用完即弃，u3-l2），跨轮复用需要把缓存、位置游标和「新消息的增量旋转」都提升到应用层管理，复杂度高得多；全量重建的正确性一目了然，对聊天场景的延迟通常可接受。

**练习 2**：`add_assistant_header=True` 追加的 `<|im_start|>assistant\n` 起什么作用？
**答案**：它是生成提示（generation prompt）——把序列「摆」在轮到助手说话的位置，模型据此接着产生助手口吻的内容；没有它，模型可能续写下一个 `<|im_start|>user` 而不是回答。

**练习 3**：如果删掉第 5 步（不把助手回复写回历史），对话会退化成什么样？
**答案**：模型每轮只看到 system + 所有 user 消息，看不到自己说过什么，会重复自我介绍、丢失指代（如「如上所述」无从谈起）——多轮记忆恰恰依赖 assistant 条目回填历史。

### 4.4 上下文裁剪：`trim_input_tensor` 的预算数学

#### 4.4.1 概念说明

Qwen3 0.6B 的上下文窗口 `context_length = 40960` 是**硬预算**：一旦前向时 `pos_end` 超过它，模型直接抛 `ValueError`（u2-l5）。多轮历史无限增长，迟早撞墙；就算不撞墙，把窗口塞满也会让每轮 prefill 越来越慢。`trim_input_tensor` 用一行不等式解决：**prompt 保留长度 + 生成预算 ≤ 上下文窗口**，超了就从左边（最旧一侧）砍。

为什么从左砍而不是从右砍？因为对话的**最新内容信息量最大**——用户刚问的问题必须在场，砍掉的是最老的轮次（甚至 system 消息）。在 token id 张量上切片（`[:, -keep_len:]`）永远切在合法 token 边界上，不会产生「半个词」。

#### 4.4.2 核心流程

关键洞察来自 u3-l2 的「跳过最后一次前向」优化：生成 \(N\) 个新 token 时，**最后一个 token 产出后不再需要前向**。设 prompt 长度为 \(L\)，生成预算为 \(N\)，上下文窗口为 \(C\)，则整个生成过程中出现过的最长序列是：

\[
L + (N - 1)
\]

（prefill 长度为 \(L\) 产出第 1 个 token；之后每产出第 \(2..N\) 个 token 前各做一次 decode 前向，最后一次前向的输入长度是 \(L + N - 1\)。）

不越界的约束是：

\[
L + N - 1 \le C \quad\Longrightarrow\quad L \le C - (N - 1)
\]

于是 prompt 的最大保留长度（`keep_len`）为：

\[
\text{keep\_len} = \max\bigl(1,\; C - (N - 1)\bigr)
\]

`max(1, ...)` 兜底保证至少留 1 个 prompt token；`max(0, N-1)` 兜底覆盖 \(N=0\) 的退化情形。用脚本默认值代入：\(C = 40960\)、\(N = 38912\)：

\[
\text{keep\_len} = 40960 - 38911 = 2049
\]

所以默认配置下，无论聊多少轮，进入模型的 prompt（历史 + 新消息）最多保留**最近 2049 个 token**——`MAX_NEW_TOKENS=38912` 这个数字正是为了把 prompt 预算精确定在 2049 而选的。

裁剪流程：

```text
trim_input_tensor(input_ids_tensor, context_len=C, max_new_tokens=N):
  1) N > C ?  → raise ValueError            # 预算本身放不进窗口，直接拒绝
  2) keep_len = max(1, C - max(0, N-1))
  3) 当前 prompt 长度 > keep_len ?           # 左截断，保留最新 keep_len 个 token
        是 → input_ids_tensor[:, -keep_len:]
        否 → 原样返回（不复制、不改动）
```

#### 4.4.3 源码精读

[qwen3_chat_interface_multiturn.py:L38-L48](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L38-L48) —— `trim_input_tensor` 全文 11 行：`ValueError` 前置校验、`keep_len` 公式（注释点明「最后一个生成 token 不需要再一次前向」）、`[:, -keep_len:]` 左截断。同一段代码逐字复制于 [chat_multiturn.py:L123-L133](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py#L123-L133)，测试因此能把两者纳入同一夹具。

[tests/test_chat_trimming.py:L15-L27](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py#L15-L27) —— pytest 夹具：用 `ast.parse` 从两个聊天应用源文件里**只摘出** `trim_input_tensor` 这个函数节点再 `exec`，避免 import 整个文件（那会触发模块级的模型权重加载，测试就跑不动了）。这是一个「测纯函数却不想背起整个应用」的实用技巧。

[tests/test_chat_trimming.py:L30-L57](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py#L30-L57) —— 参数化主测试：构造 `context_len + 4` 长的超长 prompt，断言裁剪结果恰好等于 `prompt[:, -expected_prompt_length:]`（左截断正确），再在**缩小配置的迷你 Qwen3Model**（2 层、16 维）上真的生成 `max_new_tokens` 个 token，断言产出数量正确且 `model.current_pos == context_len`——即「恰好用满预算、一步不越界」。五组参数覆盖：\(N=0\)、\(N=1\)（keep_len=C）、\(C=16,N=9\)（keep_len=8）、\(N=C\)（keep_len=1）、\(C=1,N=1\)（极小窗口兜底）。

[tests/test_chat_trimming.py:L59-L67](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py#L59-L67) —— 两个边界：未超预算时**原对象原样返回**（`is prompt`，零拷贝）；`max_new_tokens=17 > context_len=16` 时 `pytest.raises(ValueError)` 拒绝。

#### 4.4.4 代码实践

**实践目标**：手算 + 代码验证 `keep_len` 公式，再跑通官方回归测试。全程不需要 Chainlit、不需要下载 0.6B 权重。

1. 实践目标：建立「预算表」直觉，确认 \(L + N - 1 \le C\) 恒成立。
2. 操作步骤：

   ```python
   # 示例代码：verify_trim.py
   import torch

   def trim_input_tensor(input_ids_tensor, context_len, max_new_tokens):
       if max_new_tokens > context_len:
           raise ValueError("max_new_tokens must not exceed context_len.")
       keep_len = max(1, context_len - max(0, max_new_tokens - 1))
       if input_ids_tensor.shape[1] > keep_len:
           input_ids_tensor = input_ids_tensor[:, -keep_len:]
       return input_ids_tensor

   C = 40960
   for N in (0, 1, 100, 38912, 40960):
       keep = max(1, C - max(0, N - 1))
       print(f"N={N:>6}  keep_len={keep:>6}  最长序列 L+N-1={keep + N - 1}（应 ≤ {C}）")

   prompt = torch.arange(5000).unsqueeze(0)          # 模拟 5000 token 的超长历史
   trimmed = trim_input_tensor(prompt, context_len=C, max_new_tokens=38912)
   print(trimmed.shape)                               # 期望 torch.Size([1, 2049])
   print(torch.equal(trimmed, prompt[:, -2049:]))     # 期望 True：保留的是最新一段
   ```

   - `python verify_trim.py` 运行后，再跑官方测试：`pytest tests/test_chat_trimming.py -v`（在仓库根目录；迷你模型测试首次运行需下载分词器之外无任何大文件，CPU 即可）。
3. 需要观察的现象：`trimmed.shape == [1, 2049]`；相等断言为 `True`；pytest 显示 cli 与 chainlit 两组夹具 × 5 组参数全部通过，外加两个边界测试 PASSED。
4. 预期结果：与 4.4.2 的手算完全一致；`pytest` 若因缺依赖失败，按 README 安装 `-e ".[extra]"` 后重试。
5. 迷你模型的生成测试在有 GPU 的机器上更快，纯 CPU 也可通过（配置只有 2 层）。

#### 4.4.5 小练习与答案

**练习 1**：为什么公式减的是 \(N-1\) 而不是 \(N\)？极端情形 \(N=C=40960\) 时 `keep_len` 是多少？
**答案**：生成器「跳过最后一次前向」（u3-l2）——第 \(N\) 个 token 是由长度 \(L+N-1\) 的那次前向直接产出的，之后不再前向，所以过程中的最长序列是 \(L+N-1\)，减 \(N\) 会白白浪费一个位置的窗口。当 \(N=C\) 时 \(\text{keep\_len}=\max(1, 40960-40959)=1\)，只剩 1 个 prompt token，模型只能看见最后一个 token，对话完全失效——测试参数 `(16, 16, 1)` 锁定的正是这一极端情形。

**练习 2**：左截断可能切掉什么？切掉之后为什么模型仍能正常工作？
**答案**：最先被切掉的是最老的轮次，长历史下连 system 消息也会丢。token id 序列仍是一段合法编码（切点落在 token 边界），模型照常前向；代价是模型失去最早期的上下文与角色设定，可能「忘记」自己的身份或早期约定——这是保留长上下文能力的工程折中。

**练习 3**：测试为什么断言 `model.current_pos == context_len` 而不是 `<=`？
**答案**：这组参数（prompt 截到 keep_len、再生成满 \(N\) 个 token）恰好把窗口用到最后一个位置：结束时游标应**正好**到达 `context_len`——既证明预算算式精确无误（少算一格就到不了，多算一格就抛 `ValueError`），也反向锁死了「不能更贪」的上界。`<=` 会放过「裁过头」的回归。

## 5. 综合实践

把本讲三块内容（启动应用、多轮历史、裁剪数学）串成一个完整任务。

**任务：启动界面 → 观察多轮记忆 → 画出裁剪流程 → 写出三条结论。**

第一步：安装并启动单轮界面（需 Python 3.13——Chainlit 目前不支持 3.14，[pyproject.toml:L58-L62](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L58-L62) 用版本标记把 3.14 上的 chainlit 摘掉了）：

```bash
cd chG/01_main-chapter-code
# uv 路线（推荐，自动建 3.13 隔离环境）：
uv run --isolated --python 3.13 --extra extra chainlit run qwen3_chat_interface.py
# 或 pip 路线：python3.13 -m venv .venv && source .venv/bin/activate
#              python -m pip install -e "../..[extra]"
#              chainlit run qwen3_chat_interface.py
```

浏览器打开 `http://localhost:8000`（不自动打开就手动输入），发一条消息，确认回复是逐字流式出现的。可选进阶：设 `CHECKPOINT_PATH` 指向第 8 章蒸馏 checkpoint（用 `WHICH_MODEL = "reasoning"`），体验「换脑子不换壳」。

第二步：关闭服务，改跑多轮版 `qwen3_chat_interface_multiturn.py`，做两个实验：

- **记忆测试**：先说「我叫小明」，等回复后再问「我叫什么？」，验证历史生效。
- **换模测试**：对照单轮版问同样两个问题，确认单轮版答不出第二问（它不消费 history）。

第三步：在纸上或任何画图工具里，画出多轮版的裁剪流程图，必须包含以下节点与一条数值标注：

```text
history(+新消息) → build_prompt_from_history → TOKENIZER.encode
  → trim_input_tensor[C=40960, N=38912, keep_len=2049]
       ├─ 长度 ≤ 2049 → 原样通过
       └─ 长度 > 2049 → 左截断保留最近 2049 个 token（system/最老轮次先丢）
  → generate_text_basic_stream_cache（prefill L≤2049 + decode ≤N-1 次，最长 L+N-1=40960）
  → 流式回浏览器 → assistant 回复写回 history
```

第四步：结合 [tests/test_chat_trimming.py:L37-L57](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py#L37-L57) 写出三句话总结（参考答案）：

1. **为什么必须裁剪**：多轮历史会无限增长，而 `context_length` 是硬预算，`pos_end` 一越界模型就抛 `ValueError`（u2-l5），不裁剪的多轮应用聊到一定长度必然崩溃。
2. **在哪裁剪**：在「历史拼成 prompt 并编码之后、送进生成函数之前」这一步裁（[multiturn L126-L131](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L126-L131)），即 token id 张量上左截断到 `keep_len = max(1, C-(N-1))`。
3. **裁错会发生什么**：裁得不够（公式算错、少减了 1）→ 生成中途 `ValueError`，应用崩溃；裁得太过（如 `N=C` 时只剩 1 个 prompt token）→ 模型看不到完整问题与 system 设定，答非所问；而**不裁 system 消息只裁对话**之类的精细策略，本仓库没有实现，读者可作为改造练习。

（无 GPU/CPU 较慢时，第一、二步的界面体验可标注「待本地验证」，第三、四步纯纸面+测试即可完成。）

## 6. 本讲小结

- Chainlit 应用的骨架是「模块级一次性加载模型 + 两个回调」：`on_chat_start` 播种会话级 history，`on_message`（即 `main`）处理每条消息；`user_session` 保证多会话历史互不串扰。
- 单轮流式回复是四步流水线：编码（reasoning 分词器自动套聊天模板）→ 发空消息拿句柄 → 逐 token `stream_token`（EOS 双 token 监控、先查后推）→ `update` 定稿；底层消费的正是 u3-l2 的贪心流式生成器。
- 多轮记忆的实现朴素：每轮把整段 role/content 历史手工渲染成 Qwen 聊天模板（`<|im_start|>角色\n内容<|im_end|>` + assistant 头）再全量编码，生成完把回复写回历史；跨轮不复用 KV 缓存。注意 reasoning 分词器默认会对已包模板的文本再包一层，CLI 原型用 `apply_chat_template=False` 严格规避了这一点。
- 裁剪的预算数学：最长前向序列 \(L + N - 1\)（最后一个 token 免前向）不超过 \(C\)，得 `keep_len = max(1, C-(N-1))`；默认配置 \(C=40960, N=38912\) 恰好把 prompt 预算定在 2049。
- `tests/test_chat_trimming.py` 用 `ast` 只提取辅助函数避免加载整个应用，再用迷你模型断言「生成结束 `current_pos` 恰好等于 `context_len`」，把预算公式的正确性钉死到个位。
- 换模型的口径：改 `WHICH_MODEL`（base/reasoning）或用 `CHECKPOINT_PATH` 挂 GRPO/蒸馏 checkpoint（保持分词器匹配），界面代码零改动——「壳与脑分离」。

## 7. 下一步学习建议

- **本单元收官**：至此你已读完全部附录专题（SDPA 优化、批量推理、大模型加载、高级解析器、评测方法、聊天界面）。建议做一次总复盘：从 `chG` 界面发一条消息，沿着「Chainlit 回调 → 流式生成器 → KV 缓存 → GQA → RoPE」一路向下讲到底层张量，检验自己能否不留断点地走完整条链。
- **继续阅读源码**：把 [ch02/05_use_model/chat_multiturn.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py) 的 REPL 完整读一遍（含 `\clear`/`\history` 命令与 `generate_stats` 吞吐统计），它与 Chainlit 版共享同一套裁剪与模板函数，是很好的「无框架对照组」。
- **动手改造练习**（自选难度）：
  1. 给 Chainlit 多轮版加一个 `\summary` 风格按钮：点击后打印当前 history 的 token 总数与距离 2049 预算的余量；
  2. 修复 4.3.1 的双重包装问题：把 `TOKENIZER.encode(prompt)` 改为 `TOKENIZER.encode(prompt, chat_wrapped=False)`，对比修改前后的回复质量（小模型上差异可能不明显，重点是养成核对 token 序列的习惯）；
  3. 实现「system 消息豁免」裁剪：左截断时永远保留 history[0]，只裁剪后续轮次。
- **下一讲**：u10-l7 测试体系与持续集成——本讲已经见过 `tests/test_chat_trimming.py` 的 ast 技巧，下一讲系统讲解 tests 目录组织、conftest 公共夹具与 GitHub Actions 多平台 CI，为给项目贡献代码做最后准备。
