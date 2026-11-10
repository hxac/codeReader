# 构建聊天界面（Chainlit）

> 单元十 · 工程实践与附录专题 · 附录 G
> 前置讲义：u3-l2（流式生成与生成函数族）；建议同时回顾 u2-l5（KV Cache 与因果掩码）、u2-l6（Qwen3Tokenizer 与聊天模板）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清一个 Chainlit 应用的**三级执行频率**：哪些代码整个进程只跑一次（模型加载）、哪些每个会话跑一次（`on_chat_start`）、哪些每条消息跑一次（`on_message`）。
2. 读懂 `chG` 的两个界面脚本：单轮版 `qwen3_chat_interface.py` 与多轮记忆版 `qwen3_chat_interface_multiturn.py`，理解它们如何复用第 2 章的 `generate_text_basic_stream_cache` 做逐 token 流式回复。
3. 手推 `trim_input_tensor` 的**裁剪预算公式**，解释多轮历史为什么必须裁剪、在哪里裁剪、裁错了会发生什么。
4. 会用 `CHECKPOINT_PATH` 环境变量把 GRPO 训练（第 7 章）或蒸馏（第 8 章）得到的自定义 checkpoint 接进聊天界面，并让分词器配置与之对齐。

## 2. 前置知识

- **Chainlit**：一个开源的 Python Web 聊天框架（与 Streamlit 同类，但专为对话应用设计）。你只写几个带装饰器的回调函数，它负责 HTTP 服务器、浏览器页面和消息渲染。它对 Python 版本有硬性要求：**目前需要 Python 3.13，尚不支持 3.14**（[README.md:L18](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md#L18)）。这一点也写进了依赖声明——`pyproject.toml` 的 `extra` 可选组里 chainlit 带着 `python_version < '3.14'` 的环境标记（[pyproject.toml:L58-L62](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L58-L62)），所以在 3.14 上装 `extra` 会静默跳过它。
- **回调（callback）与事件驱动**：你不主动调用 Chainlit，而是把函数注册给它、由它在恰当时机调用。本讲的三个时机：`chainlit run` 启动时 **import 你的脚本**（模块级代码此时执行）、每个新浏览器会话触发 `@chainlit.on_chat_start`、用户每发一条消息触发 `@chainlit.on_message`。
- **`user_session`**：Chainlit 提供的按会话隔离的键值存储。同一服务器上多个用户各自持有独立的 `user_session`，互不串扰——多轮对话历史就存在这里。
- **聊天模板回顾（u2-l6）**：Qwen3 的对话格式是 `<|im_start|>角色\n内容<|im_end|>\n` 的拼接，生成前再补一个 `<|im_start|>assistant\n` 头。`Qwen3Tokenizer.encode` 是否自动包装由参数 `chat_wrapped` 决定，不传时**回退到构造时的 `apply_chat_template` 开关**（[qwen3.py:L389-L398](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L389-L398)）。本讲模块二会看到这个回退带来的一处微妙交互。
- **上下文长度限制回顾（u2-l5）**：`QWEN_CONFIG_06_B` 的 `context_length` 是 40 960（[qwen3.py:L15-L17](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L15-L17)）；前向时位置游标 `pos_end` 一旦越过它直接抛 `ValueError`（[qwen3.py:L66](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L66)）。这是本讲裁剪逻辑存在的根本原因。
- **流式生成回顾（u3-l2）**：`generate_text_basic_stream_cache` 是 Python 生成器，prefill 整段 prompt 后逐 token `yield`，并且「最后一个 token 生成后跳过前向」——这条优化正是模块三裁剪公式里 `-1` 的来源。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [chG/01_main-chapter-code/qwen3_chat_interface.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py) | 单轮聊天界面：每条消息独立作答，不记历史 |
| [chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py) | 多轮版：历史存 `user_session`，含 `build_prompt_from_history` 模板拼接与 `trim_input_tensor` 裁剪 |
| [chG/01_main-chapter-code/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md) | 安装（Python 3.13）、启动命令、`CHECKPOINT_PATH` 用法 |
| [tests/test_chat_trimming.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py) | 裁剪函数的回归测试（用 AST 只加载辅助函数，不加载权重） |
| [ch02/05_use_model/chat_multiturn.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py) | 命令行版多轮 REPL，与 Chainlit 版共享同一套拼接/裁剪函数（被同一测试覆盖） |
| [reasoning_from_scratch/ch02.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py) | 被 UI 复用的流式生成器 `generate_text_basic_stream_cache` |
| [reasoning_from_scratch/ch03.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py) | `load_model_and_tokenizer` / `load_tokenizer_only` 两个加载入口 |

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**① `on_start`/`main` 回调骨架与单轮流式回复（含模型加载与切换）**、**② 多轮变体：会话历史与手动聊天模板**、**③ 对话历史裁剪 `trim_input_tensor`**。

### 4.1 模块一：Chainlit 回调骨架、模型加载与单轮流式回复

#### 4.1.1 概念说明

一个 Chainlit 应用的代码分两层，执行频率完全不同：

- **模块层（进程级单例）**：`chainlit run app.py` 启动服务器前要先 import 你的脚本，模块顶端代码此时执行一次。模型和分词器就在这一层加载——**整个进程只加载一次，所有会话共享**。这澄清一个常见误解：模型并不是在回调里「懒加载」的；`on_chat_start` 懒初始化的是**每个会话自己的对话历史**，模型早在服务器启动时已就位。对 0.6B 模型这是一次几秒到几十秒的启动开销，换来的是每条消息的响应不再付加载成本。
- **回调层**：`@chainlit.on_chat_start` 每个新会话执行一次；`@chainlit.on_message` 每条用户消息执行一次。回调是 `async` 的——流式推送 token 时要 `await` 让出控制权，浏览器端才能实时刷新。

单轮版脚本把「收到一条消息 → 流式回复」压缩成四步：编码输入 → 创建可流式写入的出站消息 → 逐 token 生成并推送 → 定稿。

#### 4.1.2 核心流程

```text
chainlit run qwen3_chat_interface.py
  └─ import 脚本（进程级，只执行一次）
       ├─ 读取配置块（WHICH_MODEL / MAX_NEW_TOKENS / LOCAL_DIR / CHECKPOINT_PATH / COMPILE）
       ├─ DEVICE = get_device()
       ├─ MODEL, TOKENIZER = load_app_model_and_tokenizer()   ← 权重进内存
       └─ EOS_TOKEN_IDS = (<|im_end|> 的 id, <|endoftext|> 的 id)

每个新会话 → on_start：user_session 里建 history = [system 消息]
每条用户消息 → main：
  1) TOKENIZER.encode(message.content) → 张量 (1, L)
  2) out_msg = chainlit.Message(""); await out_msg.send()   ← 先发空气泡
  3) for tok in generate_text_basic_stream_cache(...):
        命中 EOS → break；否则 decode 单 token → await out_msg.stream_token(piece)
  4) await out_msg.update()   ← 气泡定稿
```

#### 4.1.3 源码精读

**（a）配置块与模型加载。** 脚本顶部是一段「EDIT ME」配置（[qwen3_chat_interface.py:L24-L35](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L24-L35)）：`WHICH_MODEL` 在 `"reasoning"`/`"base"` 间切换；`MAX_NEW_TOKENS = 38912`（为什么是这个怪数字，模块三揭晓）；`CHECKPOINT_PATH` 从环境变量读取——README 解释了原因：`chainlit run` 自己占用了命令行参数，脚本没法用 `argparse`，只好改走环境变量（[README.md:L69-L71](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md#L69-L71)）。

加载函数有两条互斥分支（[qwen3_chat_interface.py:L41-L63](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L41-L63)）：

- **默认分支**（未设 `CHECKPOINT_PATH`）：直接委托第 3 章的 `load_model_and_tokenizer`，按 `WHICH_MODEL` 下载并加载官方 0.6B base 或 reasoning 权重。
- **checkpoint 分支**：先 `load_tokenizer_only` 只把分词器下载到 `LOCAL_DIR`（不再下默认权重，见 [README.md:L85](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md#L85)）；然后 `Qwen3Model(QWEN_CONFIG_06_B)` 新建空模型，`torch.load(checkpoint_path, map_location="cpu")` 读入 `.pth` 文件（一个纯 `state_dict`，见 u8-l5），`load_state_dict` 灌权重，`.to(DEVICE)` 搬设备；`COMPILE=True` 时再套 `torch.compile`（并先设 `allow_unspec_int_on_nn_module`，原因见 u3-l3）。这正是把第 7 章 GRPO 或第 8 章蒸馏 checkpoint 变成可对话服务的通道——README 特别提醒：第 8 章蒸馏 checkpoint 配的是 reasoning 分词器，所以 `WHICH_MODEL` 要保持 `"reasoning"`（[README.md:L83-L84](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md#L83-L84)）。

注意 `MODEL, TOKENIZER = load_app_model_and_tokenizer()` 写在模块顶层（[qwen3_chat_interface.py:L66](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L66)）——这就是「进程级单例」的落点。

**（b）EOS 用元组而不是单个 id。** [qwen3_chat_interface.py:L70-L73](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L70-L73) 把 `<|im_end|>` 和 `<|endoftext|>` 两个特殊 token 的 id 装进元组，注释说明：官方分词器的 `eos_token_id` 二者只居其一（reasoning 用前者、base 用后者，见 u2-l6），但**自定义 checkpoint 两种都可能吐**。而生成器的 `eos_token_id` 参数只接受单个 id，所以界面脚本干脆不传它，改在流式循环里自己做「命中任一即停」的双 EOS 检查。

**（c）两个回调。** `on_start` 在 `user_session` 里初始化 `history` 并放入一条 system 消息（[qwen3_chat_interface.py:L76-L81](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L76-L81)）。注意单轮版的 `main` 其实**没有消费**这份 history——它是为多轮版预留的骨架；单轮版每条消息都是无记忆的独立问答。

**（d）`main` 的四步。** [qwen3_chat_interface.py:L84-L110](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L84-L110)：

- 第 1 步 `TOKENIZER.encode(message.content)`（L90）：这里编码的是**裸用户文本**。它能工作，是因为 reasoning 分支的 `load_model_and_tokenizer` 用 `apply_chat_template=True, add_generation_prompt=True, add_thinking=True` 构造分词器（[ch03.py:L63-L76](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L63-L76)），`encode` 会在外面包一层完整聊天模板（用户块 + assistant 生成头）。
- 第 2 步先发一个空 `chainlit.Message`（L94-L95），之后往里流式注入。
- 第 3 步循环消费生成器（L98-L107）：`tok.squeeze(0).item()` 把 `(1,1)` 张量化为 Python int；命中 `EOS_TOKEN_IDS` 就 break（EOS 本身不显示）；`TOKENIZER.decode([token_id])` 解出文本片段；`await out_msg.stream_token(piece)` 推给浏览器。`decode` 保留特殊 token（u2-l6），所以 reasoning 模型的 `<think>` 流程会原样显示在界面上——README 所说的「thinking mode」即由此而来。
- 第 4 步 `await out_msg.update()` 定稿气泡（L110）。

**（e）被复用的生成器。** [ch02.py:L116-L141](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L116-L141) 的要点（细节见 u3-l2）：`@torch.inference_mode()` 包裹；每次调用**新建** `KVCache` 并 `reset_kv_cache()` 清零位置游标（L125-L126），函数自带「自愈」；prefill 一次拿末位 logits（L128），此后每步只对 1 个新 token 前向；贪心 `argmax`（L130）；`yield` 后若已达 `max_new_tokens` 就跳过最后一次前向（L138-L139）。UI 层的每一轮对话都白拿这套 prefill + decode 两阶段与「跳过末次前向」优化。

#### 4.1.4 代码实践

1. **实践目标**：跑通单轮聊天界面，亲眼看流式输出与 thinking 模式。
2. **操作步骤**：
   - 按附录 G README 进入目录并准备 Python 3.13 环境（[README.md:L16-L42](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md#L16-L42)）：pip 路线是 `python3.13 -m venv .venv && source .venv/bin/activate && python -m pip install -e "../..[extra]"`；uv 路线更省事，第 3 步一条命令即可。
   - 启动：`chainlit run qwen3_chat_interface.py`，或 `uv run --isolated --python 3.13 --extra extra chainlit run qwen3_chat_interface.py`（[README.md:L55-L65](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md#L55-L65)）。浏览器没自动打开就手动访问终端里打印的地址（通常是 `http://localhost:8000`）。
   - 发两条消息，例如「My favorite color is blue.」和紧接着的「What is my favorite color?」。
3. **需要观察的现象**：首条消息前有明显的加载等待（进程启动时已在付加载成本）；回复逐字出现（`stream_token` 生效）；reasoning 模型先吐 `<think>…</think>` 思考流再给正式回答；第二条消息**不记得**第一条的内容（无记忆）。
4. **预期结果**：界面可正常对话、流式渲染；第二问答不出颜色，证明这是单轮版。
5. 首次运行需下载约 1.2 GB 权重；本实践依赖浏览器与本机端口，具体表现**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：把 `WHICH_MODEL` 改成 `"base"` 后，`TOKENIZER.encode(message.content)` 编码出来的还是聊天模板格式吗？为什么？

**答案**：不再是。base 分支构造分词器时不传那三个模板开关（[ch03.py:L53-L61](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L53-L61)），`Qwen3Tokenizer` 默认不套聊天模板，编码的就是裸文本。base 模型本来就是用普通文本续写方式训练的，这样反而对齐。

**练习 2**：为什么界面脚本不把 `TOKENIZER.eos_token_id` 传给生成器的 `eos_token_id` 参数，而要在循环里自己检查 `EOS_TOKEN_IDS`？

**答案**：生成器参数只装得下**一个** eos id（且内部要求 `torch.all(next_token == eos_token_id)` 才停，[ch02.py:L132-L134](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L132-L134)）；而加载自定义 checkpoint 时模型可能吐 `<|im_end|>` 也可能吐 `<|endoftext|>`，只盯一个会停不下来，所以脚本用元组在循环里双检（多轮版里那行传参干脆被注释掉了，见 [qwen3_chat_interface_multiturn.py:L142](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L142)）。

**练习 3**：如果用户往单轮界面粘贴了一篇超过 2049 个 token 的长文会发生什么？（提示：单轮版没有调用任何裁剪。）

**答案**：prompt 长度 \(L\) 加上生成预算会超出 `context_length = 40960`，`Qwen3Model.forward` 的越界检查直接抛 `ValueError`（[qwen3.py:L66](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L66)），该条消息报错（Chainlit 会在界面上显示异常）。2049 这个阈值来自 \(40960 - 38912 + 1\)，模块三给出推导。

### 4.2 模块二：多轮变体——会话历史与手动聊天模板

#### 4.2.1 概念说明

「多轮记忆」的本质很朴素：**每轮都把完整对话历史拼进 prompt 再编码**，模型每次仍做一次普通的条件续写，只是条件越来越长。要让续写发生在正确的位置，prompt 末尾要补一个 `<|im_start|>assistant\n` 生成头，让模型接着「扮演助手」往下写。

实现上有两条路线：让分词器自动套模板（单轮版的做法），或**手动拼接模板**（多轮版的做法）。多轮版必须手动拼，因为 `Qwen3Tokenizer._wrap_chat` 只会把一段文本当成**单条** user 消息包装（[qwen3.py:L411-L419](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L411-L419)），表达不了 system + 多轮 user/assistant 交替的结构。命令行版 `chat_multiturn.py` 把这一点写得很明白：它构造分词器时显式 `apply_chat_template=False`，注释「模板稍后手动加」（[chat_multiturn.py:L65-L69](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py#L65-L69)）。

> **留意一处源码交互（待本地验证）**：Chainlit 多轮版的 `TOKENIZER` 来自 `load_model_and_tokenizer`，reasoning 分支下 `apply_chat_template=True`；而 `encode` 在不传 `chat_wrapped` 时会回退到这个开关（[qwen3.py:L389-L398](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L389-L398)）。也就是说，按代码逻辑，已手工拼好模板的 prompt 会被 `_wrap_chat` 再包进一层 user 块（「双重包装」）。`WHICH_MODEL="base"` 时开关为 False 则无此问题。模块二的实践会带你实测确认——这正是「自动模板 × 手动模板」两套机制叠加时容易踩的坑。

#### 4.2.2 核心流程

history 的完整生命周期：

```text
on_start（每会话一次）
  history = [ {"role":"system", "content":"You are a helpful assistant."} ]
        │
每条消息 main（五步）：
  0) history.append(user 消息)                     ← 从 user_session 取出、追加
  1) prompt = build_prompt_from_history(history)   ← 手动拼聊天模板 + assistant 头
     input_ids = TOKENIZER.encode(prompt)
     input_ids_tensor = trim_input_tensor(...)     ← 模块三的裁剪
  2) out_msg = chainlit.Message(""); await send()
  3) 流式循环：yield token → 双 EOS 检查 → decode → stream_token
  4) await out_msg.update()
  5) history.append(assistant 回复); 写回 user_session
```

#### 4.2.3 源码精读

**（a）手动模板拼接。** [qwen3_chat_interface_multiturn.py:L51-L63](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L51-L63) 把每条 `{"role", "content"}` 渲染成 `<|im_start|>{role}\n{content}<|im_end|>\n`，最后可选追加 `<|im_start|>assistant\n` 生成头。特殊 token 之所以能被逐个正确编码，是因为 `encode` 内部的 `_SPLIT_RE` 会把它们切出来查表（u2-l6），普通片段才走 BPE。

**（b）`main` 的五步。** [qwen3_chat_interface_multiturn.py:L112-L155](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L112-L155)：

- 第 0 步（L118-L119）：从 `user_session` 取 history、追加 user 消息。
- 第 1 步（L122-L131）：拼 prompt → `encode` → 张量 → **裁剪**。`context_len` 取自 `MODEL.cfg["context_length"]`（40 960），`max_new_tokens` 用脚本级 `MAX_NEW_TOKENS`。裁剪夹在「编码完」与「开始生成」之间——这正是「在哪裁」的答案。
- 第 3 步流式循环（L138-L148）与单轮版同构，细节差异：`token_id = tok.squeeze(0)` 保留张量、用 `.tolist()` 喂 `decode`（L144、L147），而单轮版是 `.item()` 取 int——两种写法等价，`decode` 收 list 即可。
- 第 5 步（L154-L155）：把 `out_msg.content`（定稿后的完整回复文本）以 assistant 角色追加进 history 并写回 `user_session`。**没有这一步就没有记忆**。

**（c）命令行孪生版。** [chat_multiturn.py:L108-L133](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py#L108-L133) 定义了同名的 `build_prompt_from_history` 与 `trim_input_tensor`（逐字符相同）；它的 REPL 还提供 `\clear`（清空历史但保留 system，L199-L208）与 `\history`（数一数已存几轮，L209-L213）两个命令，并在每轮结束后打印 `generate_stats` 吞吐（L172-L178）。读它更适合单步调试，因为不经过浏览器。

#### 4.2.4 代码实践

1. **实践目标**：看清多轮 prompt 的真实样子与逐轮膨胀速度；实测「双重包装」是否发生。
2. **操作步骤**：
   - 不启动服务器，直接在 `chG/01_main-chapter-code` 目录写一个离线小脚本（示例代码，仅需已下载的分词器文件）：

     ```python
     # 示例代码：inspect_multiturn_prompt.py
     from reasoning_from_scratch.ch03 import load_tokenizer_only
     from reasoning_from_scratch.qwen3 import Qwen3Tokenizer
     import json, re

     # 只下载/加载分词器，不碰模型权重
     # 与 load_model_and_tokenizer 的 reasoning 分支同配置：
     tok = Qwen3Tokenizer(
         tokenizer_file_path="qwen3/tokenizer-reasoning.json",
         apply_chat_template=True, add_generation_prompt=True, add_thinking=True)

     # 从 chG 脚本原样复制这两个函数（或按 tests 的 AST 法导入）
     def build_prompt_from_history(history, add_assistant_header=True):
         parts = [f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in history]
         if add_assistant_header:
             parts.append("<|im_start|>assistant\n")
         return "".join(parts)

     history = [{"role": "system", "content": "You are a helpful assistant."}]
     for user_text, reply in [("Hi there!", "Hello! How can I help?"),
                              ("What is 2+2?", "2+2 equals 4.")]:
         history.append({"role": "user", "content": user_text})
         prompt = build_prompt_from_history(history)
         ids = tok.encode(prompt)
         print(f"--- 第 {len(history)//2} 轮：prompt {len(ids)} tokens ---")
         print(tok.decode(ids))          # 原样解码回看
         history.append({"role": "assistant", "content": reply})
     ```

   - 运行 `python inspect_multiturn_prompt.py`（分词器文件需先存在；可先调用 `download_qwen3_small(kind="reasoning", tokenizer_only=True, out_dir="qwen3")`，见 u1-l3）。
3. **需要观察的现象**：每轮 prompt 文本按 system → user → assistant → user 交替增长；token 数逐轮上升；`decode(ids)` 的输出里若出现**嵌套的** `<|im_start|>user\n<|im_start|>system…`（整个多轮结构被套进一个 user 块、末尾又多出一个 assistant 头），即证实了 4.2.1 所述的双重包装。
4. **预期结果**：token 数单调增长（这就是必须裁剪的直接证据）；是否双重包装取决于分词器开关组合——把 `apply_chat_template=True` 改为 `False` 再跑一遍，对比两次 `decode(ids)` 的差异。运行结果**待本地验证**。
5. 想对照「正确姿势」：把脚本里 tok 的构造换成 `apply_chat_template=False`（即 CLI 版做法），解码结果应只剩一层模板。

#### 4.2.5 小练习与答案

**练习 1**：如果把第 5 步（把 assistant 回复写回 history）删掉，界面会有什么表现？

**答案**：模型仍能「看到」当前这轮的 user 消息并作答（第 0 步已追加），但下一轮拼 prompt 时 history 里没有历次 assistant 回复，对话退化成「只听你说、不记自己说过什么」的半记忆状态；更严重的是 prompt 末尾的 assistant 生成头前永远只有 user 消息，模型对自己上一轮说过的话完全没有约束，多轮连贯性丢失。

**练习 2**：`build_prompt_from_history` 里 `add_assistant_header=False` 时拼出的 prompt 会长什么样？生成还能正常进行吗？

**答案**：prompt 以最后一个 `<|im_end|>\n` 结尾，即停在上一个回合的边界上，没有 `<|im_start|>assistant\n` 这个「轮到助手说话」的信号。续写位置不再明确指向助手角色，模型可能接着伪造新的 user 消息或 system 消息，输出格式失控。当前 `main` 调用固定传 `True`（L122），该参数主要为复用留出灵活性。

**练习 3**：CLI 版 `chat_multiturn.py` 的 `\clear` 命令为什么要特意保留 system 条目而不是清空整个列表？

**答案**：清空的目的是遗忘对话内容、释放上下文预算；而 system 消息是行为约束（「你是有帮助的助手」），与具体对话无关且很短，保留它不占多少预算却能让-reset 后的对话仍保持人设（[chat_multiturn.py:L199-L208](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py#L199-L208)）。列表为空时它还会补一条默认 system，保证结构完整。

### 4.3 模块三：对话历史裁剪 `trim_input_tensor`

#### 4.3.1 概念说明

多轮历史每轮都在变长，而模型的注意力窗口是硬上限：`context_length = 40960`，前向越界直接 `ValueError`（[qwen3.py:L66](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L66)）。所以**必须在「prompt 长度 + 生成预算」即将超限时丢掉一部分旧历史**。

本仓库的策略是最简单有效的一种：**token 级左截断**——预算装不下时，只保留序列**最右端**（最新）的 `keep_len` 个 token。它不感知消息边界：system 提示位于序列最左端，是最先被丢掉的内容；截断点也可能落在某条消息中间。这与「保留 system + 最近 N 轮」的消息感知裁剪不同，后者是本讲综合实践的改进方向。

#### 4.3.2 核心流程

关键是为生成预留预算。回顾 u3-l2 的生成器行为：prefill 消耗 \(L\) 个位置；此后**每个还要继续的 token 各消耗 1 个位置**；最后一个 token 产出后不再前向。于是生成长度为 \(N\) 的总位置消耗是 \(L + (N-1)\)，不越界的条件是：

\[ L + (N - 1) \le C \quad\Longleftrightarrow\quad L \le C - (N - 1) \]

其中 \(C\) 是 `context_len`。这就是 `keep_len` 的来历：

\[ \text{keep\_len} = \max\!\bigl(1,\; C - \max(0,\, N-1)\bigr) \]

两个防御：`max(0, N-1)` 防 \(N=0\) 时算出 \(C+1\)（反而允许超长 prompt）；外层 `max(1, …)` 保证至少保留 1 个 token，模型不会拿到空输入。

代入脚本默认值 \(C = 40960\)、\(N = 38912\)：

\[ \text{keep\_len} = 40960 - 38911 = 2049 \]

谜底揭晓：`MAX_NEW_TOKENS = 38912` 不是随便选的，它等价于说「给对话历史留 2049 个 token 的窗口，其余全部留给模型输出」——对会写长思考流的 reasoning 模型，这相当于「尽量生成到自然停止（EOS）」。

```text
trim_input_tensor(input_ids_tensor, context_len, max_new_tokens)
  ├─ N > C ？ → raise ValueError（预算本身就装不下）
  ├─ keep_len = max(1, C - max(0, N-1))
  └─ L > keep_len ？ → input_ids_tensor[:, -keep_len:]   ← 左截断，保最新
```

#### 4.3.3 源码精读

**（a）裁剪函数本体。** [qwen3_chat_interface_multiturn.py:L38-L48](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L38-L48)：先做预算合法性检查（`max_new_tokens` 超过 `context_len` 时无论 prompt 多短都必然越界，直接报错）；再按上式算 `keep_len`；仅当 prompt 真的超长才截断，**不超长时原张量原样返回**（后面测试用 `is` 验证这一点）。调用点在 [L127-L131](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L127-L131)，即编码之后、生成之前。

**（b）测试如何「不加载模型地」测界面脚本。** [tests/test_chat_trimming.py:L15-L27](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py#L15-L27) 的夹具是个巧活：`chat_multiturn.py` 与 Chainlit 多轮版脚本都在**模块顶层加载模型**，直接 import 会拖起整个权重加载（chainlit 版还要 import chainlit）。于是测试用 `ast.parse` 解析源码，只挑出名为 `trim_input_tensor` 的函数定义节点，单独 `compile + exec` 进一个空命名空间——**只加载这 11 行函数，脚本其余部分完全不执行**。夹具参数化覆盖 CLI 版与 Chainlit 版两个文件，保证两份拷贝永远同步。

**（c）三组断言。**

- [test_trimmed_prompt_uses_full_context（L30-L57）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py#L30-L57) 用一个缩小配置（`context_len` 可低到 1）的随机权重小模型做五组参数化验证，三连断言：截断结果恰为 `prompt[:, -expected_prompt_length:]`（保尾不保头）；流式生成恰好产出 `max_new_tokens` 个 token（没被越界打断）；生成结束后 `model.current_pos == context_len`（预算不多不少正好用满——注释点明这是「在不溢出的前提下保留尽可能多的历史」）。五组参数与 `keep_len` 公式逐一对应：\((16,0)\to16\)、\((16,1)\to16\)、\((16,9)\to8\)、\((16,16)\to1\)、\((1,1)\to1\)。
- [test_prompt_with_room_is_unchanged（L59-L61）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py#L59-L61)：短 prompt 原对象直接返回（`is` 同一性，连拷贝都没有）。
- [test_budget_exceeding_context_is_rejected（L64-L67）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py#L64-L67)：\(N > C\) 抛 `ValueError`，且报错文案可被 `match` 匹配。

#### 4.3.4 代码实践

1. **实践目标**：把 `keep_len` 公式算实；跑通裁剪回归测试；回答「为什么裁、在哪裁、裁错会怎样」。
2. **操作步骤**：
   - 用纯 Python 核对五组参数（示例代码）：

     ```python
     # 示例代码：verify_keep_len.py
     cases = [(16, 0), (16, 1), (16, 9), (16, 16), (1, 1), (40960, 38912)]
     for C, N in cases:
         keep = max(1, C - max(0, N - 1))
         print(f"context_len={C:>5}  max_new={N:>5}  keep_len={keep:>5}")
     ```

     输出应依次为 16、16、8、1、1、2049，与测试参数表的期望 prompt 长度一一对应。
   - 在仓库根目录运行 `pytest tests/test_chat_trimming.py -v`。该测试只需 torch 与本包，模型是随机初始化的小配置，**不需要下载权重、不需要 GPU**。
   - 顺手做破坏性实验：把 `keep_len` 里的 `N-1` 改成 `N`（在测试里临时复制一份函数即可，不要改源码），再跑 `pytest tests/test_chat_trimming.py -v`。
3. **需要观察的现象**：6 个参数组合全部通过（2 个夹具 × 3 个测试函数）；改坏公式后 `test_trimmed_prompt_uses_full_context` 失败——要么截掉太多（`current_pos < context_len`），要么截太少直接越界报错。
4. **预期结果**：健康版全绿；破坏版必红。这正是回归测试的意义：任何人改动裁剪逻辑，这组测试立刻拦住「差一」错误。pytest 是否可用取决于环境，**待本地验证**。
5. 用一句话记录三问的答案（见下一小节参考答案）。

#### 4.3.5 小练习与答案

**练习 1**（为什么必须裁剪、在哪裁剪、裁错会发生什么——请先自己写三句，再对照）：

**答案**：
- **为什么**：多轮历史无界增长，而模型前向有 `context_length=40960` 的硬上限，prompt 加生成预算一旦超限，`Qwen3Model.forward` 直接抛 `ValueError`，聊天当场崩掉。
- **在哪**：在 `main` 的第 1 步——历史拼成 prompt、`encode` 成 token 张量**之后**，调用生成器**之前**（[qwen3_chat_interface_multiturn.py:L127-L131](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L127-L131)）；裁剪对象是 token 张量，不是原始文本。
- **裁错会怎样**：留太多 → 生成中途越界 `ValueError`；留太少 → 白白丢弃可用历史、模型「失忆」加重；若从右侧截断（保旧弃新）→ 最新的 user 消息和 assistant 生成头被切掉，模型答非所问甚至伪造别人的发言；若漏掉 `-1`（多减一次）→ 属于「留太少」，测试的 `current_pos == context_len` 断言会抓住它。

**练习 2**：为什么公式里是 `N-1` 而不是 `N`？

**答案**：生成器「最后一个 token 产出后跳过前向」（[ch02.py:L138-L139](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L138-L139)）：第 \(N\) 个（最后一个）token 只被 `yield`，不再消耗新位置。总位置消耗是 \(L + (N-1)\)，所以预算只需给 \(N-1\) 个生成位置留量。多减一个虽然安全，但会浪费一个 token 的历史窗口；测试用 `current_pos == context_len` 把「刚好用满」钉死为规范。

**练习 3**：`MAX_NEW_TOKENS = 38912` 时，多轮对话最多能保留多长的历史？超过后最先被丢掉的是什么？

**答案**：2049 个 token（\(40960 - 38912 + 1\)）。最先被丢的是序列最左端的内容——即 system 提示，然后是最早的对话轮次；截断是 token 级的，还可能把某条旧消息切成半句。若业务上必须保住 system 提示，就需要改造成消息感知裁剪（综合实践任务）。

## 5. 综合实践

把三个模块串起来的任务：**给你的多轮界面装上「上下文水位表」，并升级成消息感知裁剪**。

1. **实践目标**：在真实界面上可视化裁剪行为；把 token 级左截断改造成「永远保住 system + 最近若干完整轮次」的裁剪。
2. **操作步骤**：
   - 复制 `qwen3_chat_interface_multiturn.py` 为 `qwen3_chat_interface_watermark.py`（放同目录，属你的实验代码）。
   - 在裁剪调用前后各加一行打印（示例代码）：

     ```python
     # 示例代码：插在 trim_input_tensor 调用处
     before = input_ids_tensor.shape[1]
     input_ids_tensor = trim_input_tensor(
         input_ids_tensor=input_ids_tensor,
         context_len=MODEL.cfg["context_length"],
         max_new_tokens=MAX_NEW_TOKENS,
     )
     print(f"[watermark] prompt {before} -> {input_ids_tensor.shape[1]} tokens, "
           f"budget keep_len={MODEL.cfg['context_length'] - MAX_NEW_TOKENS + 1}")
     ```

   - 启动 `chainlit run qwen3_chat_interface_watermark.py`，连续聊十几轮，观察终端里 prompt token 数爬升，直到某轮触发截断（数字突然回落到 2049）。
   - 升级裁剪：写一个 `trim_history_messages(history, keep_len)`，先恒保留 `role=="system"` 的条目，再从最新一轮（user/assistant 成对）往前装填，直到预算用尽；让它输出 token 数不超过 `keep_len`（可用 `TOKENIZER.encode` 试编码计量）。把它接进 `main`，在 `build_prompt_from_history` **之前**对 history 裁剪，替换原 token 级截断。
   - 画一张多轮裁剪流程图：`history（会话级）→ 消息感知裁剪（你的新函数）→ build_prompt_from_history → encode → [仍超长? → trim_input_tensor 兜底] → 生成`。
3. **需要观察的现象**：水位打印逐轮上涨；触发截断的那轮，旧消息消失但最新提问与 assistant 头仍在，模型应答仍然连贯；升级版里 system 消息在任何轮次都不再被丢弃。
4. **预期结果**：token 级版与消息感知版在「不崩溃」上等价，差别在于后者的截断边界永远落在消息边界上。界面运行表现**待本地验证**。
5. **进阶（可选）**：从第 8 章下载一个蒸馏 checkpoint，用 `CHECKPOINT_PATH=/path/to/model.pth chainlit run qwen3_chat_interface_watermark.py` 启动（`WHICH_MODEL` 保持 `"reasoning"`，见 [README.md:L76-L85](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md#L76-L85)），对比它与默认 reasoning 权重的回答风格。

## 6. 本讲小结

- Chainlit 应用有三级执行频率：模块层代码（模型加载）进程内一次、`on_chat_start`（初始化 `user_session` 里的 history）每会话一次、`on_message`（编码→流式生成→写回历史）每条消息一次；模型是进程级共享单例，不是每消息加载。
- 单轮版靠 reasoning 分词器的 `apply_chat_template=True` 自动包装裸文本；多轮版因需要表达 system + 多轮交替结构而手动 `build_prompt_from_history`，两种模板机制不可混用而不自知——`encode` 的 `chat_wrapped` 默认回退是隐藏交互点。
- 双 EOS 元组（`<|im_end|>`/`<|endoftext|>`）在流式循环里手工检查，因为生成器的 `eos_token_id` 参数只收单个 id，而自定义 checkpoint 两种都可能吐。
- 多轮记忆的本质是每轮把完整历史拼进 prompt 重新编码，代价是 prompt 无界增长；裁剪公式 \(\text{keep\_len} = \max(1, C - \max(0, N-1))\) 为生成预留预算，`-1` 来自生成器跳过最后一次前向；默认配置下历史窗口是 2049 个 token。
- `trim_input_tensor` 是 token 级左截断：保最新、弃最旧，不感知消息边界（system 最先被丢）；`tests/test_chat_trimming.py` 用 AST 只提取这 11 行函数做测试，同时钉住 CLI 版与 Chainlit 版两份拷贝的同步。
- `CHECKPOINT_PATH` 环境变量 + `load_tokenizer_only` + `load_state_dict` 三件套，把第 7 章 GRPO、第 8 章蒸馏的 `.pth` checkpoint 变成可对话的 Web 服务，分词器配置必须与 checkpoint 的训练来源对齐。

## 7. 下一步学习建议

- 下一讲是 **u10-l7 测试体系与持续集成**：本讲的 `test_chat_trimming.py` 已经预热了它的两个主题——夹具设计（这里是用 AST 隔离模块级副作用）与参数化测试；去 `tests/` 里看看每章一个测试文件的组织方式和 CI 矩阵。
- 想继续深挖「把模型变成服务」：回看 u10-l2（批量推理），思考聊天界面如果同时服务多个用户会话，生成循环该如何批量化；再对照 u9-l5 的 wrapper/export 两种 Hugging Face 互通路线，比较它们与本讲「手写 Chainlit 界面」在生态位上的差异。
- 动手方向：完成综合实践的消息感知裁剪后，可以继续给它加「显示 thinking 折叠区」（把 `<think>…</think>` 渲染成可折叠块，Chainlit 的 step 机制可用），这会让你对 u2-l6 的特殊 token 与本讲的流式管线都更熟。
