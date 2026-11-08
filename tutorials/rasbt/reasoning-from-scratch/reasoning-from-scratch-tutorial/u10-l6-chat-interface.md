# 构建聊天界面（Chainlit）

> 单元十 · 工程实践与附录专题 · 附录 G
> 前置讲义：u3-l2（流式生成与生成函数族）、u2-l5（KV Cache 与因果掩码）、u2-l6（Qwen3Tokenizer 与聊天模板）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清一个 Chainlit 应用的三级执行频率：哪些代码整个进程只跑一次（模型加载）、哪些每个会话跑一次（`on_chat_start`）、哪些每条消息跑一次（`on_message`）。
2. 读懂 `chG` 的两个界面脚本：单轮版 `qwen3_chat_interface.py` 与多轮记忆版 `qwen3_chat_interface_multiturn.py`，并理解它们如何复用第 2 章的 `generate_text_basic_stream_cache` 做逐 token 流式输出。
3. 手推 `trim_input_tensor` 的裁剪预算公式，解释为什么多轮历史必须裁剪、在哪里裁剪、裁错了会发生什么。
4. 会用 `CHECKPOINT_PATH` 环境变量把 GRPO 训练或蒸馏得到的自定义 checkpoint 接进聊天界面。

## 2. 前置知识

- **Chainlit**：一个开源的 Python Web 聊天框架（类似 Streamlit，但专为对话应用设计）。你只写几个带装饰器的回调函数，它负责 HTTP 服务器、浏览器页面和消息渲染。注意它对 Python 版本有要求：**目前需要 Python 3.13**，还不支持 3.14（见 [README.md:L18](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md#L18)）。
- **回调（callback）**：你不主动调用 Chainlit，而是把函数注册给它、由它在合适的时机调用。本讲涉及三个装饰器外的关键机制：`chainlit.run` 启动时会 **import 你的脚本**（模块级代码此时执行）、每个新浏览器会话触发 `@chainlit.on_chat_start`、用户每发一条消息触发 `@chainlit.on_message`。
- **`user_session`**：Chainlit 提供的按会话隔离的键值存储。同一服务器上多个用户各自拿到独立的 `user_session`，互不串扰——多轮对话历史就存在这里。
- **聊天模板回顾（u2-l6）**：Qwen3 的对话格式是 `<|im_start|>角色\n内容<|im_end|>\n` 的拼接，生成前还要补一个 `<|im_start|>assistant\n` 头。`Qwen3Tokenizer.encode` 是否自动包装由 `apply_chat_template` 开关决定（[qwen3.py:L389-L398](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L389-L398)）。
- **上下文长度限制回顾（u2-l5）**：`Qwen3Model` 的 `context_length` 是 40 960（[qwen3.py:L17](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L17)）；位置游标 `current_pos` 越过它，前向直接抛 `ValueError`。这是本讲裁剪逻辑存在的根本原因。
- **流式生成回顾（u3-l2）**：`generate_text_basic_stream_cache` 是一个 Python 生成器，prefill 整段 prompt 后逐 token `yield`，且「最后一个 token 生成后跳过前向」——这条优化正是裁剪公式里 `-1` 的来源。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [chG/01_main-chapter-code/qwen3_chat_interface.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py) | 单轮聊天界面：每条消息独立作答，不记历史 |
| [chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py) | 多轮版：历史存入 `user_session`，含 `trim_input_tensor` 裁剪与 `build_prompt_from_history` 模板拼接 |
| [chG/01_main-chapter-code/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md) | 安装（Python 3.13）、启动命令、`CHECKPOINT_PATH` 用法 |
| [tests/test_chat_trimming.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py) | 裁剪函数的回归测试（用 AST 只加载辅助函数，不加载权重） |
| [ch02/05_use_model/chat_multiturn.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py) | 命令行版多轮 REPL，与 Chainlit 版共享同一套裁剪/拼接函数（被同一测试覆盖） |
| [reasoning_from_scratch/ch02.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py) | 被 UI 复用的流式生成器 `generate_text_basic_stream_cache` |
| [reasoning_from_scratch/ch03.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py) | `load_model_and_tokenizer` / `load_tokenizer_only` 两个加载入口 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**① Chainlit 回调骨架与单轮流式回复**、**② 多轮变体：会话历史与手动聊天模板**、**③ 上下文裁剪 `trim_input_tensor`**。

### 4.1 模块一：Chainlit 回调骨架与单轮流式回复

#### 4.1.1 概念说明

一个 Chainlit 应用的代码分两层：

- **模块层（进程级单例）**：`chainlit run app.py` 启动服务器时要先 import 你的脚本，模块顶端的代码此时执行一次。模型和分词器就在这里加载——**整个进程只加载一次，所有用户共享**。这澄清一个常见误解：模型并不是在 `on_chat_start` 里"懒加载"的；`on_chat_start` 懒初始化的是**每个会话的对话历史**，模型早在服务器启动时就已经就位。
- **回调层**：`@chainlit.on_chat_start` 每个新会话执行一次；`@chainlit.on_message` 每条用户消息执行一次。回调是 `async` 的，流式推送 token 时要让出控制权（`await`），UI 才能实时刷新。

单轮版脚本把整条链路压缩到四步：编码输入 → 创建可流式写入的出站消息 → 逐 token 流式生成 → 定稿。

#### 4.1.2 核心流程

```text
chainlit run qwen3_chat_interface.py
  └─ import 脚本（进程级，执行一次）
       ├─ 读取配置块（WHICH_MODEL / MAX_NEW_TOKENS / CHECKPOINT_PATH / COMPILE）
       ├─ DEVICE = get_device()
       ├─ MODEL, TOKENIZER = load_app_model_and_tokenizer()   ← 权重进内存
       └─ EOS_TOKEN_IDS = (<|im_end|>, <|endoftext|>)

每个浏览器会话：
  └─ on_chat_start()：user_session["history"] = [system 消息]

每条用户消息：
  └─ main(message)：
       1) TOKENIZER.encode(message.content) → tensor
       2) out_msg = chainlit.Message(""); await out_msg.send()
       3) for tok in generate_text_basic_stream_cache(...):
            命中 EOS_TOKEN_IDS → break
            TOKENIZER.decode([token_id]) → await out_msg.stream_token(piece)
       4) await out_msg.update()
```

#### 4.1.3 源码精读

**配置块**（[qwen3_chat_interface.py:L21-L35](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L21-L35)）：`WHICH_MODEL = "reasoning"` 选 reasoning 变体（改成 `"base"` 即切换基座模型）；`MAX_NEW_TOKENS = 38912` 看似惊人，实为与 40 960 上下文配合的"尽量生成到顶"上限（第 4.3 节展开）；`CHECKPOINT_PATH` 从环境变量读取——因为 `chainlit run` 占用了命令行参数位，脚本无法用 `argparse`，只能走环境变量（[README.md:L69-L78](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md#L69-L78)）。

**模型加载的双分支**（[qwen3_chat_interface.py:L41-L63](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L41-L63)）：

```python
def load_app_model_and_tokenizer():
    if CHECKPOINT_PATH is None:
        return load_model_and_tokenizer(...)      # 分支一：默认权重
    checkpoint_path = Path(CHECKPOINT_PATH)
    if not checkpoint_path.exists():
        raise FileNotFoundError(...)              # 自定义路径先做存在性检查
    tokenizer = load_tokenizer_only(...)         # 只下载分词器
    model = Qwen3Model(QWEN_CONFIG_06_B)         # 空壳模型
    model.load_state_dict(torch.load(checkpoint_path, map_location="cpu"))
    model.to(DEVICE)
```

分支一复用第 3 章的 `load_model_and_tokenizer`（[ch03.py:L52-L90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L52-L90)），自动下载并加载 base/reasoning 权重；分支二是**接入自定义 checkpoint 的通道**：`load_tokenizer_only` 只下载分词器不下载默认权重（[ch03.py:L93-L113](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L93-L113)），模型按 `QWEN_CONFIG_06_B` 新建空壳再 `load_state_dict` 灌权重——这正是 u8-l5（GRPO checkpoint）和 u9-l5（蒸馏 checkpoint）讲过的「先建同构模型再灌 state_dict」套路。README 特别提醒：第 8 章蒸馏 checkpoint 配 reasoning 分词器，`WHICH_MODEL` 必须保持 `"reasoning"`（[README.md:L81-L86](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md#L81-L86)）。

**进程级加载点**（[qwen3_chat_interface.py:L66](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L66)）：`MODEL, TOKENIZER = load_app_model_and_tokenizer()` 写在模块顶层、任何回调之外——它在 `chainlit run` import 脚本时执行，先于一切用户交互。

**EOS 双保险**（[qwen3_chat_interface.py:L68-L73](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L68-L73)）：

```python
EOS_TOKEN_IDS = (
    TOKENIZER.encode("<|im_end|>")[0],
    TOKENIZER.encode("<|endoftext|>")[0]
)
```

官方约定 reasoning 变体以 `<|im_end|>` 收尾、base 变体用 `<|endoftext|>`，但注释点明「一些自定义 checkpoint 两种都会吐」——比如用 base 权重蒸馏出来的模型。所以 UI 层把两个停止符都收进元组，在生成循环里手动判断，而不是依赖生成函数的 `eos_token_id` 参数（多轮版 L142 处该参数被注释掉，就是刻意留用 UI 层判断）。

**回调与流式循环**（[qwen3_chat_interface.py:L76-L110](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L76-L110)）：

```python
@chainlit.on_chat_start
async def on_start():
    chainlit.user_session.set("history", [])
    chainlit.user_session.get("history").append(
        {"role": "system", "content": "You are a helpful assistant."})

@chainlit.on_message
async def main(message: chainlit.Message):
    input_ids = TOKENIZER.encode(message.content)
    ...
    for tok in generate_text_basic_stream_cache(model=MODEL, token_ids=input_ids_tensor, ...):
        token_id = tok.squeeze(0).item()
        if token_id in EOS_TOKEN_IDS:
            break
        piece = TOKENIZER.decode([token_id])
        await out_msg.stream_token(piece)
    await out_msg.update()
```

三个细节值得注意：

1. **单轮版其实也初始化了 history（含 system 消息），但 `main` 从不读它**——直接 `encode(message.content)`。这是有意的最简实现，多轮版才消费 history。
2. 默认 `WHICH_MODEL="reasoning"` 时，`load_model_and_tokenizer` 返回的分词器带 `apply_chat_template=True, add_generation_prompt=True, add_thinking=True`（[ch03.py:L71-L76](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L71-L76)），所以裸文本 `message.content` 经 `encode` 会被自动包上聊天模板与换行起始的 assistant 头（模型自行写 `<think>`）。
3. **EOS 不入显示**：`break` 在 `decode` 之前，停止符不会作为文本流给用户；`out_msg.update()` 把流式状态定稿为一条完整消息。

#### 4.1.4 代码实践

**实践目标**：用"启动即失败"的方式亲眼确认模型是在**服务器启动时（模块 import）**加载，而不是在第一条消息时加载。

**操作步骤**（不下载任何权重，失败得很快）：

1. 按 [README.md:L26-L40](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md#L26-L40) 在 `chG/01_main-chapter-code` 下装好 chainlit 环境（需 Python 3.13）。
2. 不设 `CHECKPOINT_PATH` 先正常启动一次确认可跑通后，改用一个不存在的路径启动：
   ```bash
   CHECKPOINT_PATH=/tmp/definitely-not-here.pth \
   uv run --isolated --python 3.13 --extra extra \
     chainlit run qwen3_chat_interface.py
   ```
3. 观察报错时机与内容。

**需要观察的现象**：终端在**启动阶段**（还没打开浏览器、没发任何消息）就抛出 `FileNotFoundError: Checkpoint file not found: /tmp/definitely-not-here.pth`——它来自 [qwen3_chat_interface.py:L51-L52](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L51-L52) 的存在性检查，证明模块级代码在 import 期已执行。

**预期结果**：服务器直接启动失败。若你想进一步观察"正常路径下进程只加载一次"，可下载好权重后启动服务、连续发多条消息，对比首条消息与其后消息的响应延迟（首条不含加载差异时应当接近；权重加载发生在打开页面前）。

**待本地验证**：chainlit 安装与浏览器交互需本地环境，本讲不预设已运行。

#### 4.1.5 小练习与答案

**练习 1**：把 `WHICH_MODEL` 从 `"reasoning"` 改成 `"base"`，除了权重文件不同，分词器行为还有什么变化？

**答案**：base 分支构造的 `Qwen3Tokenizer` 不带任何聊天模板参数（[ch03.py:L61](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L61)），`apply_chat_template` 默认为 `False`（[qwen3.py:L359-L362](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L359-L362)），所以 `encode(message.content)` 得到的是裸文本 token，没有 `<|im_start|>` 包装——模型按续写方式作答。

**练习 2**：为什么停止判断用元组 `EOS_TOKEN_IDS` 而不是生成函数的 `eos_token_id` 参数？

**答案**：`eos_token_id` 只接受**单个** id，而自定义 checkpoint（如 base 权重蒸馏出的模型）可能吐 `<|im_end|>` 也可能吐 `<|endoftext|>`；元组在 UI 层同时兜住两种停止符（[qwen3_chat_interface.py:L68-L73](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L68-L73) 注释原话："some custom checkpoints emit both"）。

**练习 3**：`on_start` 里写入的 history 在单轮版中有没有用？

**答案**：没有。单轮版 `main` 只消费 `message.content`（L90），history 写入后从未被读取；它是从多轮版复用来的骨架，多轮版的 `main` 才在第 0 步读它。

### 4.2 模块二：多轮变体——会话历史与手动聊天模板

#### 4.2.1 概念说明

"多轮记忆"的本质：**每一轮都把完整对话历史重新编码成一个 prompt 喂给模型**。语言模型本身没有跨请求的记忆，每条回答只依赖本轮输入；所谓记忆，就是应用层替模型保管历史、每轮重拼。要解决两个问题：

1. **格式**：历史要按 Qwen3 聊天模板拼成模型认识的形态——这是 `build_prompt_from_history` 的职责。
2. **存放**：历史按会话隔离地存在 `user_session`，而不是全局变量（否则多用户互相污染）。

#### 4.2.2 核心流程

```text
用户第 k 次发消息：
  0) history = user_session.get("history")          # 取出 [system, u1, a1, ..., u(k-1), a(k-1)]
     history.append({"role": "user", "content": 本条消息})
  1) prompt = build_prompt_from_history(history)     # 拼成 <|im_start|>...<|im_end|>...assistant\n
     input_ids = TOKENIZER.encode(prompt)
     input_ids_tensor = trim_input_tensor(...)       # 超长则左截断（模块三）
  2) out_msg = Message(""); await send()
  3) 流式生成（同单轮版，命中 EOS_TOKEN_IDS 停）
  4) await out_msg.update()
  5) history.append({"role": "assistant", "content": out_msg.content})
     user_session.set("history", history)            # 回写，供下一轮使用
```

与单轮版相比只有三处增量：第 0 步的取历史与追加用户消息、第 1 步的手动模板拼接与裁剪、第 5 步的助手回复回写。

#### 4.2.3 源码精读

**手动聊天模板**（[qwen3_chat_interface_multiturn.py:L51-L63](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L51-L63)）：

```python
def build_prompt_from_history(history, add_assistant_header=True):
    parts = []
    for m in history:
        parts.append(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n")
    if add_assistant_header:
        parts.append("<|im_start|>assistant\n")
    return "".join(parts)
```

每条消息套 `<|im_start|>角色\n内容<|im_end|>\n` 外壳后顺序拼接，末尾补 `<|im_start|>assistant\n` 作为"轮到助手说话"的生成起点。为什么不复用分词器的聊天模板？因为它只会包装**单条**用户消息（`_wrap_chat` 只接受一个 `user_msg`，见 [qwen3.py:L411-L419](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L411-L419)），装不下多轮历史，所以多轮版自己拼字符串。

> **源码阅读发现（值得亲自验证）**：`WHICH_MODEL="reasoning"`（默认值）时，分词器带 `apply_chat_template=True`，而 `main` 调用 `TOKENIZER.encode(prompt)` 时**没有**传 `chat_wrapped=False`（[qwen3_chat_interface_multiturn.py:L121-L124](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L121-L124)）。按 [qwen3.py:L389-L398](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L389-L398) 的逻辑，`encode` 默认值 `chat_wrapped=None` 会回落到 `self.apply_chat_template=True`，于是手动拼好的整段模板会被 `_wrap_chat` **再包一层** user 消息——即"模板套模板"。特殊 token 本身仍会被正确编码成 id（`_SPLIT_RE` 分割保证），生成也能进行，但格式嵌套是否影响对话质量需实测；若想严格单层包装，可在 encode 时显式传 `chat_wrapped=False`（属示例性修改建议，源码未这样写）。该行为对 `WHICH_MODEL="base"` 不存在（base 分词器 `apply_chat_template=False`）。

**会话状态的读写**（[qwen3_chat_interface_multiturn.py:L117-L119 与 L153-L155](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L117-L119)）：第 0 步取出 history 并追加 `{"role": "user", ...}`；第 5 步把 `out_msg.content`（流式聚合后的完整回复文本）作为 assistant 消息追加回去并 `set` 回会话。注意 `user_session.set` 存的是**列表对象引用**——因为第 0 步取出的就是同一个列表，第 5 步的 `set` 更多是显式表达的防御性写法。

**同一逻辑的 CLI 孪生**：[ch02/05_use_model/chat_multiturn.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py) 是命令行 REPL 版，`build_prompt_from_history`（[L108-L120](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py#L108-L120)）与 `trim_input_tensor`（[L123-L133](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py#L123-L133)）逐字相同，history 换成模块级列表（[L101-L105](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py#L101-L105)），并多了 `\clear`（清空但保留 system，[L199-L208](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py#L199-L208)）与 `\history` 命令。两份实现被同一个测试文件覆盖（见 4.3），这是"一套逻辑、两个前端"的典型工程做法。

#### 4.2.4 代码实践

**实践目标**：不启动 UI、不加载权重，直接观察"三轮对话拼出的 prompt 长什么样"。

**操作步骤**：`build_prompt_from_history` 是纯函数，把它抄进一个脚本（以下为**示例代码**，可与 [multiturn 版 L51-L63](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L51-L63) 对照）：

```python
# 示例代码：独立运行，无需 torch / chainlit
def build_prompt_from_history(history, add_assistant_header=True):
    parts = [f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in history]
    if add_assistant_header:
        parts.append("<|im_start|>assistant\n")
    return "".join(parts)

history = [{"role": "system", "content": "You are a helpful assistant."}]
for user, assistant in [("我叫小明", "你好，小明！"), ("我叫什么？", "你叫小明。")]:
    history += [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
history.append({"role": "user", "content": "1+1=?"})
print(build_prompt_from_history(history))
```

**需要观察的现象**：输出是一段连续文本——system 块在最前，两轮问答依次排开，最后以 `<|im_start|>assistant\n` 结尾；**没有** `<think>` 标签（手动模板不管思考格式，reasoning 模型会自己写）。

**预期结果**：能数出 3 个 `<|im_start|>` 开头的完整消息块（1 system + 2 轮 ×2 条）加 1 个开头的 assistant 头。若你已通过 `download_qwen3_small(kind="reasoning", tokenizer_only=True)` 拿到分词器，可再调用 `Qwen3Tokenizer(...).encode(prompt)` 并 `decode` 回来，亲眼验证 4.2.3 提到的"模板套模板"现象（decode 结果里出现嵌套的 `<|im_start|>user\n<|im_start|>system...` 即为实证；此步**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：多轮版 `main` 的第 5 步为什么必须在 `out_msg.update()` 之后才把回复写入 history？

**答案**：`out_msg.content` 在流式过程中不断增长，`update()` 定稿后它才是完整回复；先写 history 再 update 的话，若流中断，history 里存的是残缺文本（阅读 [L150-L155](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L150-L155) 的语句顺序可见 update 在前、append 在后）。

**练习 2**：为什么多轮版要自己写 `build_prompt_from_history`，而不是像单轮版那样直接 `encode(message.content)`？

**答案**：单轮版只有一条消息，分词器内置的 `_wrap_chat` 够用；多轮历史是"多条不同角色的消息"，`_wrap_chat` 只接受单条 user 消息（[qwen3.py:L411-L419](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L411-L419)），装不下，所以必须在应用层手动拼整段模板。

**练习 3**：如果把 history 存成模块顶层的全局列表（像 CLI 版那样），Web 版会发生什么？

**答案**：Chainlit 服务器是多用户共享一个进程的，全局列表会让所有用户的对话互相混入对方的 history；`user_session` 按会话隔离（每个浏览器会话各自一份），CLI 版没有这个问题才用全局列表。

### 4.3 模块三：上下文裁剪 `trim_input_tensor`

#### 4.3.1 概念说明

多轮历史每轮只增不减，而模型的 `context_length` 是硬上限（40 960）：一旦某次前向的 `pos_end` 越界，`Qwen3Model.forward` 直接抛 `ValueError`（u2-l5 精读过这段守卫）。因此**必须**在把历史送进生成函数前做窗口裁剪。`trim_input_tensor` 就是这把剪刀，它回答一个问题：**给定要生成 `max_new_tokens` 个新 token，prompt 最多允许保留多长？**

关键直觉有两条：

1. **预算要给生成留座位**：prompt 长度 + 生成占用 ≤ 上下文长度。生成 \( N \) 个 token 并不需要 \( N \) 次前向——最后一个 token 产出后不需要再前向（u3-l2 的"跳过最后一次前向"），所以生成实际只占用 \( N-1 \) 个新位置。
2. **截断要保尾部（左截断）**：`[:, -keep_len:]` 保留**最靠后**的 token。最新的用户问题与结尾的 `<|im_start|>assistant\n` 头必须活着；反之右截断会把"当前问题"切掉，模型接着旧内容续写，答非所问。

#### 4.3.2 核心流程

```text
trim_input_tensor(input_ids_tensor, context_len, max_new_tokens):
  1) 守卫：max_new_tokens > context_len → ValueError（预算本身超窗，无解）
  2) keep_len = max(1, context_len - max(0, max_new_tokens - 1))
  3) 若 prompt 长度 > keep_len：左截断，只保留最后 keep_len 个 token
     否则：原样返回（同一个张量对象，不复制）
```

预算的数学表达：

\[
\text{prompt\_len} + (\text{max\_new\_tokens} - 1) \;\le\; \text{context\_len}
\quad\Longrightarrow\quad
\text{keep\_len} = \text{context\_len} - \text{max\_new\_tokens} + 1
\]

两个边界防护：`max(0, ...)` 防止 `max_new_tokens=0` 时算出负数反而放大预算；`max(1, ...)` 保证至少留 1 个 prompt token（模型总得有输入）。代入脚本默认值：\( 40960 - 38912 + 1 = 2049 \)——**无论聊了多少轮，模型每轮实际能"看见"的窗口只有 2049 个 token**，这就是 `MAX_NEW_TOKENS=38912` 的真实含义：把剩余上下文几乎全部让渡给生成长度。

还有一个容易被忽略的设计事实：左截断保留的是**最后 keep_len 个 token**，而 system 消息位于整段 prompt 的**最前端**——历史一长，**最先被裁掉的恰恰是 system 提示**。裁剪的是"模型的视野"，不是 `user_session` 里的 history（后者永不丢失）。

#### 4.3.3 源码精读

**裁剪函数本体**（[qwen3_chat_interface_multiturn.py:L38-L48](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L38-L48)）：

```python
def trim_input_tensor(input_ids_tensor, context_len, max_new_tokens):
    if max_new_tokens > context_len:
        raise ValueError("max_new_tokens must not exceed context_len.")
    # The last generated token does not need another forward pass.
    keep_len = max(1, context_len - max(0, max_new_tokens - 1))
    if input_ids_tensor.shape[1] > keep_len:
        input_ids_tensor = input_ids_tensor[:, -keep_len:]   # 左截断：保留尾部
    return input_ids_tensor
```

四行逻辑：守卫 → 预算（注释点明 `-1` 的来由正是"最后一个 token 无需前向"）→ 超长才截断 → 否则原张量直接返回。

**调用点**（[qwen3_chat_interface_multiturn.py:L121-L131](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L121-L131)）：先把整段历史编码成张量，再以 `context_len=MODEL.cfg["context_length"]`、`max_new_tokens=MAX_NEW_TOKENS` 调用裁剪——**位置在编码之后、生成之前**，裁的是输入张量这一份"拷贝的视野"。

**生成端的配合**：`generate_text_basic_stream_cache` 每次调用新建 KVCache 并清零 `current_pos`（[ch02.py:L117-L139](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L117-L139)），prefill 消费 `prompt_len` 个位置、此后每个非末位 token 各进一格。所以整个生成结束时的位置游标恰为 `keep_len + (max_new_tokens - 1) = context_len`，**不多不少恰好填满**——测试正是用这一点做断言。

**回归测试**（[tests/test_chat_trimming.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py)）分三段：

1. **AST 抽取夹具**（[L15-L27](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py#L15-L27)）：直接 import 聊天脚本会触发模块级的整模型加载，于是测试用 `ast.parse` 只摘出 `trim_input_tensor` 这个函数节点单独 `exec`——一个"只加载辅助函数、不加载权重"的巧技。参数化同时覆盖 CLI 版与 Chainlit 版，锁死"两份实现必须行为一致"。
2. **主测试**（[L30-L57](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py#L30-L57)）：五组参数 \((\text{context\_len}, \text{max\_new\_tokens}, \text{期望保留长度})\) = (16,0,16)、(16,1,16)、(16,9,8)、(16,16,1)、(1,1,1)，构造长度为 `context_len+4` 的超长 prompt，三重断言：
   - 裁剪结果等于 `prompt[:, -expected:]`（截的是尾部）；
   - 生成恰好产出 `max_new_tokens` 个 token（预算没把生成挤瘪）；
   - 生成后 `model.current_pos == context_len`（**恰好填满、绝不越界**——注释写明 "Keep as much history as possible without overflowing during generation"）。
3. **边界测试**（[L59-L67](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py#L59-L67)）：不超长的 prompt 返回**同一个对象**（`is` 断言，验证"无需裁剪时零拷贝"）；`max_new_tokens=17 > context_len=16` 时抛出信息匹配的 `ValueError`。

用 (16, 9, 8) 这组手验：keep_len = 16 − 8 = 8，prompt 从 20 截到 8；生成 9 个 token 做 1 次 prefill + 8 次 decode 前向，`current_pos = 8 + 8 = 16` 恰好等于上下文。若公式漏掉 `-1`，(16,9) 会保留 7 而不是 8——测试立刻失败；若公式把预算算大，`current_pos` 会试图变成 17，越界守卫先崩。

#### 4.3.4 代码实践

**实践目标**：不加载任何权重，验证裁剪公式的五组边界，并跑通官方回归测试。

**操作步骤**：

1. 写一个小脚本（**示例代码**，函数体照抄 [multiturn 版 L38-L48](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L38-L48)）：

   ```python
   # 示例代码：只需 torch
   import torch

   def trim_input_tensor(input_ids_tensor, context_len, max_new_tokens):
       if max_new_tokens > context_len:
           raise ValueError("max_new_tokens must not exceed context_len.")
       keep_len = max(1, context_len - max(0, max_new_tokens - 1))
       if input_ids_tensor.shape[1] > keep_len:
           input_ids_tensor = input_ids_tensor[:, -keep_len:]
       return input_ids_tensor

   for ctx, mnt, expect in [(16, 0, 16), (16, 1, 16), (16, 9, 8), (16, 16, 1), (1, 1, 1)]:
       prompt = torch.arange(ctx + 4).unsqueeze(0)          # 超长 prompt
       out = trim_input_tensor(prompt, ctx, mnt)
       assert out.shape[1] == expect, (ctx, mnt, out.shape[1])
       assert torch.equal(out, prompt[:, -expect:])          # 截的是尾部
   short = torch.tensor([[4, 5]])
   assert trim_input_tensor(short, 16, 9) is short           # 不超长→原对象
   print("all checks passed")
   ```

2. 再跑官方测试（需已安装本包与 torch，权重**不会**被下载）：

   ```bash
   pytest tests/test_chat_trimming.py -v
   ```

**需要观察的现象**：示例脚本打印 `all checks passed`；pytest 中 `test_trimmed_prompt_uses_full_context` 按 5 组参数 × 2 个被测文件共 10 条全部通过，另两条边界测试通过。

**预期结果**：全部绿色。若把照抄函数里的 `-keep_len:` 改成 `:keep_len`（右截断），`torch.equal` 断言会立刻失败——这就是"裁错方向"的最小复现。

**待本地验证**：`current_pos == context_len` 那条断言需真实前向，依赖 torch 环境；其余断言纯张量运算，任何装有 torch 的环境都可复现。

#### 4.3.5 小练习与答案

**练习 1**：把脚本里的 `MAX_NEW_TOKENS` 从 38912 改成 40960 会发生什么？

**答案**：`trim_input_tensor` 第一道守卫 `max_new_tokens > context_len` 不触发（40960 == 40960 不大于），但 `keep_len = max(1, 40960 - 40959) = 1`——prompt 只剩 1 个 token，等于模型"几乎失忆"地作答。若再改大成 40961，才会抛 `ValueError`。这说明生成长度与记忆窗口是**此消彼长**共享同一个 40960 预算的。

**练习 2**：公式里 `max(0, max_new_tokens - 1)` 的 `max(0, ...)` 防的是什么？

**答案**：防 `max_new_tokens=0`。没有它，`0 - 1 = -1`，`keep_len = context_len + 1` 会把预算放大到超过上下文本身；测试参数表第一组 `(16, 0, 16)`（[L31](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py#L31)）锁死的就是这个边界。

**练习 3**：为什么测试断言用 `model.current_pos == context_len` 而不是 `<=`？

**答案**：`==` 同时锁住两个方向——大于意味着生成中途越界（会先被模型前向的 ValueError 拦下），小于意味着 prompt 被裁得过短、浪费了本可保留的历史。注释"Keep as much history as possible without overflowing"说明这是**恰好填满**的设计目标，`<=` 无法检测"过度裁剪"。

## 5. 综合实践

把三个模块串成一个完整闭环：**跑起界面 → 观察记忆 → 用测试语言总结裁剪**。

**任务 A：启动单轮界面，验证流式输出**

1. 按 [README.md:L16-L40](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md#L16-L40) 准备 Python 3.13 环境（pip 建虚拟环境装 `-e "../..[extra]"`，或直接用 uv）。
2. 从 `chG/01_main-chapter-code` 目录启动：`chainlit run qwen3_chat_interface.py`（uv 用户：`uv run --isolated --python 3.13 --extra extra chainlit run qwen3_chat_interface.py`，见 [README.md:L55-L67](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md#L55-L67)）。
3. 浏览器打开（通常 `http://localhost:8000`）问两遍"我叫小明，你记得吗？"——单轮版第二遍应当不记得。
4. 换跑 `qwen3_chat_interface_multiturn.py` 重复同样两问，第二遍它应能答出名字——这就是 `user_session` 历史 + 手动模板的效力。同时观察回复是**逐字浮现**而非整段弹出（`stream_token` 的效果）。

**任务 B：画多轮历史裁剪流程图**

通读 [qwen3_chat_interface_multiturn.py:L112-L155](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L112-L155) 后，画出如下流程（补全每个箭头旁的动作与数据形态）：

```text
user_session["history"]（永不裁剪，完整记忆）
        │ append user
        ▼
build_prompt_from_history ──► 字符串 prompt（<|im_start|>...assistant\n）
        │ TOKENIZER.encode
        ▼
token 张量（完整历史的 id）
        │ trim_input_tensor：keep_len = 40960-38911 = 2049
        │ 超长 → 左截断保留【最后 2049 个 token】（system 最先掉出窗口）
        ▼
裁剪后张量 ──► generate_text_basic_stream_cache ──► 流式 token ──► 回写 assistant 进 history
```

注意：真实实现保留的是"**最近的 token 窗口**"，并不是"system + 最近 N 轮"——system 消息没有任何特殊保护，这是流程图里最值得标注的一个设计事实。

**任务 C：结合 `tests/test_chat_trimming.py` 写三句总结**（参考答案）

1. **为什么必须裁剪**：多轮历史单调增长，而模型对超过 `context_length=40960` 的前向直接抛 ValueError（RoPE 表与位置游标都以它为界），不裁剪的聊天应用必然在某一轮崩溃。
2. **在哪裁剪**：在"整段历史编码成 token 张量之后、送入生成函数之前"这一处集中裁剪（[multiturn 版 L127-L131](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface_multiturn.py#L127-L131)），裁的是模型的输入视野，`user_session` 里的完整历史原样保留。
3. **裁错会发生什么**：方向裁反（右截断）会丢掉最新问题与 assistant 头，模型答非所问；预算算大会在生成中途越界崩溃——测试用"裁剪结果等于尾部切片 + 生成恰好 N 个 token + `current_pos == context_len`"三条断言把这两个错误方向同时锁死（[test_chat_trimming.py:L52-L56](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py#L52-L56)）。

**进阶选做**：用第 8 章的蒸馏 checkpoint 换掉默认权重（先按 u9-l5 的方式下载，再依 [README.md:L76-L79](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/README.md#L76-L79) 设置 `CHECKPOINT_PATH` 启动，`WHICH_MODEL` 保持 `"reasoning"`），对比蒸馏模型与默认 reasoning 模型在同一问题下的思考风格差异。

## 6. 本讲小结

- Chainlit 应用的代码分三级执行频率：模型/分词器在 **import 期加载一次**（进程级单例，[qwen3_chat_interface.py:L66](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chG/01_main-chapter-code/qwen3_chat_interface.py#L66)），`on_chat_start` 每会话初始化 history，`on_message` 每条消息走"编码→流式→定稿"四步。
- 流式回复 = 复用第 2 章生成器 `generate_text_basic_stream_cache` + UI 层逐 token `stream_token`；EOS 停止用 `<|im_end|>`/`<|endoftext|>` **双 id 元组**兜底，因为自定义 checkpoint 可能吐出任一种。
- 多轮记忆的本质是应用层每轮重拼完整历史：`build_prompt_from_history` 手动套 `<|im_start|>` 模板（内置模板只装得下单条消息），历史按会话隔离地存在 `user_session`；默认 reasoning 配置下 `encode` 还会再包一层模板（源码阅读发现，`chat_wrapped=False` 可避免）。
- 裁剪预算公式 \(\text{keep\_len} = \max(1,\ \text{ctx} - (\text{mnt}-1))\)，`-1` 来自"最后一个 token 无需前向"；默认参数下窗口为 2049，生成长度与记忆宽度共享 40960 总预算、此消彼长。
- 左截断保留**尾部** token——最新问题与 assistant 头必须存活，而位于最前端的 system 消息**没有任何特权**，历史一长最先被裁掉。
- `tests/test_chat_trimming.py` 用 AST 只摘取裁剪函数（不加载权重）、以"尾部切片 + 恰好 N 个 token + `current_pos == context_len`"三重断言同时覆盖 CLI 与 Chainlit 两份实现，是"恰好填满、绝不越界"设计目标的可执行规格。

## 7. 下一步学习建议

- **下一讲（u10-l7）**：测试体系与持续集成——本讲已经见过 AST 抽取夹具这个巧技，下一讲系统看 `tests/` 的组织、`conftest.py` 公共夹具与 GitHub Actions 多平台矩阵。
- **换个前端再读一遍**：[ch02/05_use_model/chat_multiturn.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py) 是同一套裁剪/拼接逻辑的 CLI 版，多了 `\clear`、`\history` 命令与 `generate_stats` 吞吐统计，对照阅读能加深"逻辑与前端分离"的理解。
- **把训练成果接进 UI**：回顾 u8-l5 的 `download_qwen3_grpo_checkpoints` 与 u9-l5 的 `download_qwen3_distill_checkpoints`，下载一个 checkpoint 后用本讲的 `CHECKPOINT_PATH` 机制加载，亲手和自己训练的模型对话。
- **延伸思考**：若要真正实现"保留 system + 最近 N 轮"的裁剪策略，需要改 `trim_input_tensor` 的输入粒度（在消息级而非 token 级裁剪）——可作为一个小改造练手，并为它补一条类似 [test_chat_trimming.py:L30-L36](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_chat_trimming.py#L30-L36) 的参数化测试。
