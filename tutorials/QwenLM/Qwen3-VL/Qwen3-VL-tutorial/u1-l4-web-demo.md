# Web Demo 与 Docker 部署

> 本讲属单元一（初识 Qwen3-VL 与环境搭建）。前置：u1-l3 第一次推理（你已经掌握 `messages` 结构与 `apply_chat_template` → `generate` → 裁剪解码这条链路）。本讲把这条链路"装进一个网页"。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `web_demo_mm.py` 的全部命令行参数及默认值，并分清"哪些参数只对哪个后端生效"。
2. 讲清 `_load_model_processor` 的双后端设计：hf 路径与 vllm 路径各自如何加载模型，vLLM 未安装时如何优雅降级。
3. 掌握 Gradio 会话历史（`task_history`）到模型 `messages` 的重组逻辑，包括按文件扩展名区分图像/视频、`messages.pop()` 的真实作用。
4. 区分两种"流式输出"：HF 路径 `TextIteratorStreamer` + 后台线程的真流式，与 vLLM 离线 API 的"伪流式"。
5. 会用官方 Docker 镜像一键启动 Web Demo，并读懂启动脚本与镜像构建文件。

## 2. 前置知识

- **Gradio**：一个用几行 Python 就能搭出网页交互界面的库。本讲用到它的四个概念——`gr.Blocks`（页面容器）、`gr.Chatbot`（对话窗口组件）、`gr.State`（每个会话私有的状态变量）、**生成器函数**（回调函数里用 `yield` 而不是 `return`，Gradio 会把每次 `yield` 的结果增量推给浏览器，天然实现流式刷新）。
- **后端（backend）**：界面不变、推理引擎可插拔。本脚本支持 `hf`（transformers 原生 `generate`）与 `vllm`（高吞吐推理引擎）两种后端。直觉上：hf 零额外依赖、适合快速验证；vllm 连续批处理、吞吐高，是官方推荐的部署方式（单元三精讲）。
- **流式输出（streaming）**：模型每生成一个 token 就立刻显示，而不是等全部生成完。transformers 提供 `TextIteratorStreamer`，把阻塞的 `generate` 变成一个可 `for` 迭代的"token 水管"。
- **线程（Thread）**：`model.generate` 是阻塞调用。把它放进后台线程去"生产" token，主线程同时从 streamer 里"消费"，这就是经典的生产者-消费者模式。
- **Docker 四件套**：镜像（image，只读模板）、容器（container，运行实例）、端口映射（`-p 宿主端口:容器端口`）、目录挂载（`--mount`，把宿主机模型权重目录挂进容器）。GPU 直通需要 `--gpus all` 与 nvidia-container-toolkit。
- **张量并行（tensor parallel）**：把单层权重切到多张 GPU 上并行计算。本讲只需认识 `--tensor-parallel-size` 这个参数，单元三再展开。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [web_demo_mm.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py) | 多模态 Web Demo 主程序（378 行） | 全部四个模块的主线：参数、双后端加载、会话重组、流式输出、界面绑定 |
| [requirements_web_demo.txt](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/requirements_web_demo.txt) | Demo 依赖清单 | 注意它**不含** vllm 与 qwen-vl-utils，这决定了默认后端为何跑不起来 |
| [docker/docker_web_demo.sh](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/docker_web_demo.sh) | 官方一键启动脚本 | 拉镜像、校验权重、端口映射、挂载、容器内启动命令 |
| [docker/Dockerfile-qwen3vl-cu128](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/Dockerfile-qwen3vl-cu128) | 官方镜像构建文件 | 镜像基于 vllm-openai，解释了容器内为何默认走 vllm 后端 |
| [README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md) | 项目门面文档 | Web UI Example 一节的官方启动说明 |

提醒：README 中 Web UI 小节（[README.md:L825-L852](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L825-L852)）提到的脚本是 `run_web_demo.sh`，但仓库里实际文件名是 `docker/docker_web_demo.sh`——文档与脚本名不一致，以仓库文件为准。

## 4. 核心概念与源码讲解

先给一张总览图，`main()` 只有三行，是全程序的主干：

```text
main()
 ├── _get_args()                  # ① 解析命令行参数
 ├── _load_model_processor(args)  # ② 按 backend 加载模型与 processor
 └── _launch_demo(args, ...)      # ③ 搭 Gradio 界面并进入事件循环
```

对应 [web_demo_mm.py:L371-L374](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L371-L374)：主函数把"参数 → 加载 → 界面"三步串起来，其余所有函数都是这三步的支撑。

### 4.1 命令行参数：_get_args

#### 4.1.1 概念说明

脚本用标准库 `argparse` 的 `ArgumentParser` 定义了 10 个参数。理解它们的关键不是背默认值，而是记住一条**参数-后端对应关系**：`--backend` 决定推理引擎，而 `--cpu-only`、`--flash-attn2` 只在 hf 分支被读取，`--gpu-memory-utilization`、`--tensor-parallel-size` 只在 vllm 分支被读取（下一节源码会印证）。

#### 4.1.2 核心流程

```text
命令行字符串
   │ argparse 解析
   ▼
args 命名空间（args.checkpoint_path / args.backend / ...）
   │
   ├── 通用：checkpoint-path、share、inbrowser、server-port、server-name、backend
   ├── 仅 hf 后端使用：cpu-only、flash-attn2
   └── 仅 vllm 后端使用：gpu-memory-utilization、tensor-parallel-size
```

#### 4.1.3 源码精读

参数定义集中在 [web_demo_mm.py:L24-L63](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L24-L63)，整理成下表：

| 参数 | 默认值 | 作用 | 生效后端 |
| --- | --- | --- | --- |
| `-c` / `--checkpoint-path` | `Qwen/Qwen3-VL-235B-A22B-Instruct` | 模型名（HF Hub id）或本地权重目录 | 两者 |
| `--cpu-only` | `False` | 纯 CPU 运行 | 仅 hf |
| `--flash-attn2` | `False` | 加载时启用 `flash_attention_2` 注意力实现 | 仅 hf |
| `--share` | `False` | 生成公网可访问的分享链接 | 通用（launch） |
| `--inbrowser` | `False` | 自动在默认浏览器打开新标签页 | 通用（launch） |
| `--server-port` | `7860` | 服务监听端口 | 通用（launch） |
| `--server-name` | `127.0.0.1` | 服务监听地址 | 通用（launch） |
| `--backend` | `vllm` | `hf`（HuggingFace）或 `vllm` | — |
| `--gpu-memory-utilization` | `0.70` | vLLM 可占用的显存比例 | 仅 vllm |
| `--tensor-parallel-size` | `None`（自动取 GPU 数） | vLLM 张量并行度 | 仅 vllm |

三个容易踩坑的默认值：

1. 默认后端是 **vllm** 而不是 hf，见 [web_demo_mm.py:L48-L52](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L48-L52)：`choices=['hf', 'vllm'], default='vllm'`。
2. 默认 checkpoint 是 **235B-A22B** 巨无霸（[web_demo_mm.py:L27-L31](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L27-L31)），学习时务必用 `-c Qwen/Qwen3-VL-2B-Instruct` 覆盖。
3. `--tensor-parallel-size` 默认 `None` 时会自动取可见 GPU 数（[web_demo_mm.py:L57-L60](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L57-L60)），下一节讲自动推断的位置。

#### 4.1.4 代码实践

1. **实践目标**：把参数表从"看过的表格"变成"自己验证过的事实"。
2. **操作步骤**：安装依赖后运行 `python web_demo_mm.py --help`，对照上表逐项核对参数名、默认值与 help 文案；再用 `grep -n "args\." web_demo_mm.py` 找出每个参数在源码中被读取的全部位置。
3. **需要观察的现象**：`--help` 输出的参数清单与上表一致；`args.` 的检索结果能显示 `cpu_only`、`flash_attn2` 只出现在 `_load_model_processor` 的 hf 分支，`gpu_memory_utilization`、`tensor_parallel_size` 只出现在 vllm 分支。
4. **预期结果**：10 个参数、参数-后端对应关系与上表完全吻合。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 `--flash-attn2` 传给 vllm 后端不会有任何效果？
答案：该参数只在 `_load_model_processor` 的 hf 分支（L96-L102）参与 `from_pretrained` 的 `attn_implementation` 选择；vllm 分支根本不读取 `args.flash_attn2`，vLLM 引擎内部自行决定注意力实现。

**练习 2**：想让局域网内另一台机器访问你的 Demo，需要哪个参数？
答案：`--server-name 0.0.0.0`（默认 `127.0.0.1` 只监听本机回环地址）；端口用 `--server-port` 改，默认 7860。Docker 脚本里容器内正是这么做的（4.5 节）。

**练习 3**：`--share` 和 `--server-name 0.0.0.0` 都能让别人访问，区别是什么？
答案：`--share` 借助 Gradio 的中转服务生成一个公网临时链接（无需公网 IP，但流量经第三方）；`0.0.0.0` 只是监听本机所有网卡，仍要求访问者能直连你的机器所在网络。

### 4.2 模型加载双后端：_load_model_processor

#### 4.2.1 概念说明

"双后端"指同一份界面代码可以插上两种推理引擎。设计上有一个工程细节值得学习：**可选依赖的容错导入**。vllm 和 qwen-vl-utils 并不是这个 Demo 的硬依赖，脚本在文件头用 `try/except ImportError` 探测它们是否存在，只置一个布尔标志、打印警告，而不让程序崩溃：

[web_demo_mm.py:L15-L21](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L15-L21)：尝试导入 vLLM 的 `SamplingParams`/`LLM` 与 `qwen_vl_utils.process_vision_info`，成功则 `VLLM_AVAILABLE = True`；失败则打印"未安装 vllm 时无法使用 vllm 后端"的警告。

这就解释了一个新手常见现象：只执行 `pip install -r requirements_web_demo.txt` 后直接运行 `python web_demo_mm.py`（默认 vllm 后端），会得到一句 ImportError——因为依赖清单 [requirements_web_demo.txt:L1-L13](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/requirements_web_demo.txt#L1-L13) 里只有 gradio、transformers（固定到 git commit `7aa888b`）、torch、torchvision、accelerate 等，**并不包含 vllm 与 qwen-vl-utils**。此时要么补装这两个包，要么 `--backend hf`。

#### 4.2.2 核心流程

```text
_load_model_processor(args)
   │
   ├── args.backend == 'vllm'
   │     ├── VLLM_AVAILABLE 为 False → 抛 ImportError（带安装提示）
   │     ├── 设环境变量 VLLM_WORKER_MULTIPROC_METHOD='spawn'
   │     ├── tensor_parallel_size 未指定 → 取 torch.cuda.device_count()
   │     ├── LLM(model=..., gpu_memory_utilization=0.70, enforce_eager=False,
   │     │        tensor_parallel_size=..., seed=0)
   │     └── AutoProcessor.from_pretrained(...) → 返回 (model, processor, 'vllm')
   │
   └── args.backend == 'hf'
         ├── device_map = 'cpu'（--cpu-only）或 'auto'
         ├── --flash-attn2 → from_pretrained(torch_dtype='auto',
         │        attn_implementation='flash_attention_2', device_map=...)
         │   否则 → from_pretrained(device_map=...)
         └── AutoProcessor.from_pretrained(...) → 返回 (model, processor, 'hf')
```

注意函数返回的是**三元组**，第三个元素是后端名字符串——它后续会被 `_launch_demo` 显示在页面标题里（L336 的 `Backend: {backend.upper()}`），让你一眼看出当前页面连的是哪个引擎。

#### 4.2.3 源码精读

vllm 分支主体在 [web_demo_mm.py:L67-L88](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L67-L88)，关键行逐一看：

- [web_demo_mm.py:L71](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L71)：把 vLLM 多进程 worker 的启动方式设为 `spawn`（每个 worker 用全新进程启动，避免 CUDA 环境下 fork 带来的问题）。
- [web_demo_mm.py:L72-L74](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L72-L74)：这就是 4.1 节说的"自动推断"——命令行没给 `--tensor-parallel-size` 时，直接取可见 CUDA 设备数。
- [web_demo_mm.py:L77-L84](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L77-L84)：初始化 vLLM 离线推理引擎 `LLM`。`enforce_eager=False` 表示允许使用 CUDA graph 加速；`seed=0` 固定随机种子保证可复现。
- [web_demo_mm.py:L87-L88](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L87-L88)：即便用 vLLM 跑推理，**processor 仍然从 transformers 加载**——因为渲染 chat template 需要它。

hf 分支在 [web_demo_mm.py:L89-L105](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L89-L105)：

- [web_demo_mm.py:L90-L93](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L90-L93)：`--cpu-only` 时 `device_map='cpu'`，否则 `'auto'`（accelerate 自动分配层到多卡）。
- [web_demo_mm.py:L96-L102](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L96-L102)：两个加载调用的唯一差异是 `--flash-attn2` 时额外传 `torch_dtype='auto'` 与 `attn_implementation='flash_attention_2'`。flash-attn 是可选依赖，requirements 里被注释掉了（[requirements_web_demo.txt:L10-L12](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/requirements_web_demo.txt#L10-L12)），不装它就不该开这个开关。
- [web_demo_mm.py:L104-L105](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L104-L105)：processor 加载后返回三元组。

顺带一提，`_gc()`（[web_demo_mm.py:L150-L154](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L150-L154)）在用户点"清除历史"时做 Python 垃圾回收并清空 CUDA 缓存，是长驻服务里释放显存的小技巧。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到"容错导入 + 按需报错"的分级降级行为。
2. **操作步骤**：在一个只装了 `requirements_web_demo.txt`（未装 vllm）的环境里，先运行 `python web_demo_mm.py --backend hf -c Qwen/Qwen3-VL-2B-Instruct`，确认能正常进入加载流程；中断后改运行 `python web_demo_mm.py -c Qwen/Qwen3-VL-2B-Instruct`（默认 vllm 后端）。
3. **需要观察的现象**：第一次启动时终端早已打印过 L21 的 Warning（vLLM not available）；第二次运行在加载阶段抛出 `ImportError: vLLM is not available. Please install vllm and qwen-vl-utils.`。
4. **预期结果**：hf 后端不依赖 vllm 可正常启动；vllm 后端的报错信息精确指向缺失的两个包。待本地验证（完整加载 2B 模型需约 4GB+ 显存或足够内存）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 vllm 分支还要 `AutoProcessor.from_pretrained`？vLLM 引擎自己不认识 chat template 吗？
答案：vLLM 引擎只接收"已渲染的 prompt + 多模态张量"。渲染模板、把图像/视频预处理成张量这些活仍由 transformers 的 processor 与 qwen-vl-utils 完成（见 4.4 节 `_prepare_inputs_for_vllm`），所以两个后端都需要 processor。

**练习 2**：单卡机器上 `--tensor-parallel-size 3` 会发生什么？
答案：张量并行度 3 大于可用 GPU 数 1，vLLM 初始化时会因无法切分权重而报错。正确取值应等于（或整除）可用 GPU 数；单卡就显式传 1，或不传走自动推断。

**练习 3**：hf 分支两个 `from_pretrained` 调用里，不开 `--flash-attn2` 时为什么连 `torch_dtype='auto'` 都不传？
答案：这是官方 Demo 的保守写法：默认路径完全使用 `from_pretrained` 的缺省行为（跟随 config 的 dtype 策略），只在你显式要求高性能注意力实现时才一并锁定 `torch_dtype='auto'`，减少可选依赖组合爆炸带来的意外。

### 4.3 会话历史到 messages 的重组：predict 与 _transform_messages

#### 4.3.1 概念说明

Gradio 界面里维护着**两份平行状态**：

- `chatbot`：给用户看的对话列表，存的是"美化后"的文本（HTML 转义、图片占位等）；
- `task_history`：给模型用的原始历史，元素是二元组——`(文本, 回答)` 或 `((文件路径,), 回答)`，未回答时回答为 `None`。

模型的 `messages`（u1-l3 学过的格式）必须从 `task_history` **重建**出来，因为 Gradio 不会替你维护 transformers 的对话结构。重建时有两条规则：

1. **文件靠扩展名识别类型**：`_is_video_file`（[web_demo_mm.py:L145-L147](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L145-L147)）用 8 个视频后缀（`.mp4/.avi/.mkv/.mov/.wmv/.flv/.webm/.mpeg`）判断，命中即 video，否则一律按 image 处理；路径会经 `os.path.abspath` 转绝对路径（Gradio 把上传文件放在临时目录）。
2. **文本充当"回合分隔符"**：遇到文本条目就把此前累积的图片/视频与这句话打包成一条 user 消息，随后补一条 assistant 消息；连续的文件条目累积进同一条 user 消息的 content。

#### 4.3.2 核心流程

用一个具体例子走一遍（图片先上传、第一轮已回答、第二轮刚提问）：

```text
task_history（Gradio 原始状态）
  [ (("/tmp/xxx/a.png",), None),            ← 上传的图片（tuple 形式）
    ("图中是什么？",   "是一只狗"),            ← 第 1 轮已完成
    ("它戴什么颜色的项圈？", None) ]           ← 本轮提问，待回答
        │ predict() 中 for q, a in history_cp 逐对遍历
        ▼
messages（重建后的中间形态，键名是简写）
  [ user:      [ {image: /abs/a.png}, {text: 图中是什么？} ]
    assistant: [ {text: 是一只狗} ]
    user:      [ {text: 它戴什么颜色的项圈？} ] ]   ← 末尾的 assistant(None) 被 pop 掉
        │ _transform_messages() 规范化键名
        ▼
messages（规范形态，带 type 字段）
  [ {'role':'user','content':[{'type':'image','image':...},{'type':'text','text':...}]}, ... ]
```

#### 4.3.3 源码精读

重建循环在 `predict` 函数内，见 [web_demo_mm.py:L254-L267](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L254-L267)：

- q 是 tuple/list（文件）→ 按扩展名 append `{'video': abs路径}` 或 `{'image': abs路径}`，**不**产生新消息；
- q 是文本 → append `{'text': q}`，然后立刻收尾：append user 消息（携带此前累积的全部 content）、append assistant 消息 `{'text': a}`，并清空 content 开始下一回合。

[web_demo_mm.py:L267](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L267) 的 `messages.pop()` 是本模块最值得琢磨的一行：循环对最后一轮提问也会照例 append 一条 assistant 消息，但此时回答还是 `None`——把它弹掉，messages 才以 user 轮收尾，正好配合后文 `add_generation_prompt=True` 让模型接话。

`_transform_messages`（[web_demo_mm.py:L157-L175](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L157-L175)）把简写键规范成 `{'type': 'image'/'text'/'video', ...}`。这正对应 u1-l3 里提到的 messages 两种合法写法：**带 `type` 的规范写法**与**直接以 `image`/`video` 为键的简写**——Gradio 侧先攒简写，喂模型前统一转规范。

两个细节：

- predict 开头（[web_demo_mm.py:L243-L250](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L243-L250)）对空输入做了防御：输入为空就把 chatbot 与历史中最后一条弹掉直接返回。
- 存回历史的回答是 `_parse_text()` 处理过的文本（[web_demo_mm.py:L269-L277](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L269-L277) 中 `full_response = _parse_text(response)`），也就是说下一轮重建 messages 时，历史回答里可能带有 `<br>` 等显示用标记。这是官方 Demo 的一个已知简化：显示层与数据层没有完全分离，自己写生产代码时建议存原始文本、只在渲染时转义。

#### 4.3.4 代码实践

1. **实践目标**：不动 Gradio，纯手工验证重建逻辑，把 4.3.2 的图变成自己推出来的结果。
2. **操作步骤**：把下面的历史写进 Python（无需模型），照 L254-L267 的循环逻辑手工/代码模拟一遍，再调用 `_transform_messages`：

   ```python
   # 示例代码（模拟 task_history，非项目原有代码）
   history = [
       (("/tmp/gradio/a.png",), None),
       ("图里有什么动物？", "有一只狗"),
       ((" /tmp/gradio/bird.mp4 ",), None),
       ("视频里的鸟在做什么？", None),
   ]
   ```

3. **需要观察的现象**：第 3 项（mp4）被归入 video 而不是 image；`a.png` 与第一句文本进入**同一条** user 消息；`bird.mp4` 与第二句文本进入另一条 user 消息；最终 messages 长度为 3（user/assistant/user）。
4. **预期结果**：重建出的 messages 为 `user([image a.png, text 图里有什么动物？])` → `assistant([text 有一只狗])` → `user([video bird.mp4, text 视频里的鸟在做什么？])`，且经 `_transform_messages` 后每个元素都带 `type` 字段。可以用脚本 `import web_demo_mm; web_demo_mm._transform_messages(msgs)` 直接验证（该文件 import gradio/torch，需先装依赖）。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果用户先发文字"你好"，得到回答后再上传图片提问，重建出的 messages 是什么样？
答案：三段——`user([text 你好])`、`assistant([text 回答])`、`user([image 绝对路径, text 新问题])`。图片条目只是累积进 content，不会单独成一条消息。

**练习 2**：把 `messages.pop()` 删掉会发生什么？
答案：messages 末尾会多一条 `assistant([{'text': None}])`。传给 `apply_chat_template` 时要么在渲染阶段报错（None 无法拼接），要么生成格式错误的模板串；`add_generation_prompt=True` 期望对话以 user 轮结束，这条多余的空 assistant 消息破坏了该前提。

**练习 3**：为什么判断文件类型只看扩展名就够用、而不用担心误判？
答案：因为上传组件在源头就限定了类型——[web_demo_mm.py:L344](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L344) 的 `gr.UploadButton(..., file_types=['image', 'video'])` 只允许选择图片与视频文件，扩展名判断只是在这两类之间做二选一。

### 4.4 Gradio 界面与流式输出

#### 4.4.1 概念说明

本模块回答两个问题：**回复怎么"流"到页面上**，以及**页面由哪些部件组成**。

- 真流式（hf 后端）：`TextIteratorStreamer` 是 transformers 提供的可迭代对象，把它传给 `model.generate` 的 `streamer` 参数后，每解码出一个新 token 它就吐出一段文本。`generate` 阻塞主线程怎么办？放到后台 `Thread` 里跑，主线程 `for new_text in streamer` 边收边 `yield` 给 Gradio。
- 伪流式（vllm 后端）：vLLM 的离线 `LLM.generate` 是"整批提交、全部生成完一次性返回"的 API，不是流式接口。代码里虽然写了 `for output in ...: yield accumulated_text` 的循环形状，但单条提问实际只在生成结束时 yield 一次——页面表现为"等一会儿、回复整段出现"。真·流式要用 vLLM 的 server + OpenAI SDK stream（单元三 u3-l3）。

#### 4.4.2 核心流程

```text
用户点击 Submit
   │ add_text：把 (text, None) 追加进 chatbot 与 task_history
   │ then(predict)：
   │     重建 messages（4.3）
   │     call_local_model(...)
   │        ├── vllm：_prepare_inputs_for_vllm → SamplingParams(max_tokens=1024)
   │        │          → LLM.generate(inputs) → 逐 output 累积 yield（伪流式）
   │        └── hf：  apply_chat_template(tokenize=True) → TextIteratorStreamer
   │                   → 后台 Thread 跑 generate → 主线程 for streamer 逐段 yield（真流式）
   │     每次 yield 更新 chatbot 最后一格 → Gradio 推给浏览器
   ▼
生成结束：task_history[-1] = (query, full_response)
```

#### 4.4.3 源码精读

vllm 路径（[web_demo_mm.py:L206-L217](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L206-L217)）：

- [web_demo_mm.py:L208-L209](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L208-L209)：先用 `_prepare_inputs_for_vllm` 组装输入，再建 `SamplingParams(max_tokens=1024)`——两个后端的生成长度上限一致，都是 1024。
- [web_demo_mm.py:L211-L217](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L211-L217)：`model.generate(inputs, ...)` 接受字典形式的 prompt（含 `prompt` 文本、`multi_modal_data` 图像/视频张量、`mm_processor_kwargs` 视频参数），返回后逐个 output 累积文本并 yield。

其中 `_prepare_inputs_for_vllm`（[web_demo_mm.py:L178-L198](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L178-L198)）是本仓库反复出现的"vLLM 输入准备三件套"模式的雏形：`apply_chat_template(tokenize=False)` 渲染纯文本提示；`process_vision_info(..., image_patch_size=processor.image_processor.patch_size, return_video_kwargs=True, return_video_metadata=True)` 预处理视觉输入（`image_patch_size` 与 `return_video_metadata` 是 Qwen3-VL 的新机制，单元二 u2-l1/u2-l4 精读）；最后装进 `prompt` / `multi_modal_data` / `mm_processor_kwargs` 三个键。单元三 u3-l2 会专门拆这个模式。

hf 路径（[web_demo_mm.py:L218-L239](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L218-L239)）：

- [web_demo_mm.py:L220-L226](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L220-L226)：`apply_chat_template(tokenize=True, return_dict=True, return_tensors='pt')`——正是 u1-l3 学过的调用，一步拿到 input_ids 与视觉张量。
- [web_demo_mm.py:L229](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L229)：`TextIteratorStreamer(tokenizer, timeout=20.0, skip_prompt=True, skip_special_tokens=True)`——20 秒收不到新 token 就超时；`skip_prompt=True` 跳过输入部分（等价于你在 u1-l3 手工做的"裁剪前缀"）；`skip_special_tokens=True` 不把 `<|im_end|>` 之类吐进页面。
- [web_demo_mm.py:L231-L234](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L231-L234)：张量搬到模型设备，把 `streamer` 塞进 `gen_kwargs`，用 `Thread(target=model.generate, kwargs=gen_kwargs)` 后台启动。
- [web_demo_mm.py:L236-L239](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L236-L239)：主线程迭代 streamer，累积后 yield。

界面与事件绑定（[web_demo_mm.py:L330-L368](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L330-L368)）：`gr.Chatbot` 高 500（L339）、输入框（L340）、`gr.State` 存 task_history（L341）；按钮行（[web_demo_mm.py:L343-L347](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L343-L347)）是 上传/发送/重试/清空 四件套。事件绑定（[web_demo_mm.py:L349-L354](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L349-L354)）里值得注意 Submit 用了 `.then()` 链：先 `add_text` 落账、再 `predict` 生成，同时另一个 click 清空输入框。`regenerate`（[web_demo_mm.py:L282-L301](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L282-L301)）把最后一轮的回答抹成 `None` 后复用 predict。最后 `demo.queue().launch(...)`（[web_demo_mm.py:L363-L368](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L363-L368)）启用请求排队并按 4.1 的四个 launch 参数启动服务。

显示侧还有两个小助手：`_parse_text`（[web_demo_mm.py:L108-L137](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L108-L137)）把 Markdown 代码围栏转成 HTML、转义代码内特殊字符，避免 Gradio 渲染错乱；`_remove_image_special`（[web_demo_mm.py:L140-L142](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L140-L142)）剥掉模型回复中残留的 `<ref>`/`<box>` 等视觉标注标记。

#### 4.4.4 代码实践

1. **实践目标**：对比两种后端的"流式手感"，把 4.4.1 的论断变成观察事实。
2. **操作步骤**：先用 `--backend hf` 启动 Demo（命令见第 5 节综合实践），提交一个"写一首 100 字的短诗"的纯文本请求并盯着页面；若本机装有 vllm（`pip install "vllm>=0.11.0" qwen-vl-utils`），再用默认 vllm 后端重启并重复同一请求。
3. **需要观察的现象**：hf 后端回复**逐字/逐小段**浮现；vllm 后端在等待一段时间后回复**整段一次性**出现。同时终端都打印 `User: ...` 与 `Qwen-VL-Chat: ...` 两行日志（[web_demo_mm.py:L251](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L251) 与 [web_demo_mm.py:L276](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/web_demo_mm.py#L276)）。
4. **预期结果**：两种后端最终文本一致（采样有随机性，内容不必逐字相同），差异只在呈现节奏——hf 真流式、vllm 伪流式。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`skip_prompt=True` 替代了 u1-l3 里的哪一步？
答案：替代了 `out_ids[len(in_ids):]` 的手工前缀裁剪。streamer 直接从"生成部分"开始迭代，输入 token 不会被吐出来。

**练习 2**：为什么 `TextIteratorStreamer` 要设 `timeout=20.0`？
答案：防止界面永久卡死。若生成线程异常退出（如显存不足），streamer 再也等不到新 token；有了超时，主线程会在 20 秒后收到超时异常从而结束本次请求，而不是让用户对着永远空转的页面。

**练习 3**：想让 vllm 后端也逐字输出，应该改用哪条技术路线？
答案：放弃离线 `LLM.generate`，改用 `vllm serve` 起 OpenAI 兼容服务，客户端以 `stream=True` 请求并逐 chunk 渲染——这正是单元三 u3-l3 在线服务部署的内容。

### 4.5 Docker 一键部署

#### 4.5.1 概念说明

官方把"环境 + 依赖 + Demo 脚本"打包成镜像 `qwenllm/qwenvl:qwen3vl-cu128`，启动脚本 `docker_web_demo.sh` 负责四件事：解析参数、**校验本地权重**、拉取镜像、以正确参数运行容器。它的设计前提是：vLLM 引擎、qwen-vl-utils、gradio 等都已在镜像里配好，宿主机只需要有权重和 Docker。

镜像构建文件 [docker/Dockerfile-qwen3vl-cu128](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/Dockerfile-qwen3vl-cu128) 揭示了两个关键事实：

- [Dockerfile-qwen3vl-cu128:L2-L3](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/Dockerfile-qwen3vl-cu128#L2-L3)：基础镜像是 `vllm/vllm-openai:v0.11.0`（vLLM 官方镜像），并把 `ENTRYPOINT` 清空以便执行自定义命令——所以容器里 vllm 天然可用。
- [Dockerfile-qwen3vl-cu128:L5-L11](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/Dockerfile-qwen3vl-cu128#L5-L11)：在此基础上补装 transformers（git 主分支）、`qwen-vl-utils==0.0.14`、`torchcodec==0.7`（视频解码后端，单元二 u2-l3 会遇到）、`gradio==5.49.1`、`transformers-stream-generator`、accelerate；[L14](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/Dockerfile-qwen3vl-cu128#L14) 把 `web_demo_mm.py` 拷进镜像。

#### 4.5.2 核心流程

```text
bash docker/docker_web_demo.sh -c /path/to/weights --port 8881
   ├── 解析 -i/-c/-n/--port（默认：镜像 qwen3vl-cu128、端口 8901、容器名 qwen3vl）
   ├── 检查 ${QWEN_CHECKPOINT_PATH}/config.json 存在，否则退出
   ├── sudo docker pull ${IMAGE_NAME}
   └── sudo docker run --gpus all -d --restart always --name ${CONTAINER_NAME}
        -p ${PORT}:80
        --mount type=bind,source=权重目录,target=/data/shared/Qwen/checkpoint
        python web_demo_mm.py --server-port 80 --server-name 0.0.0.0
               -c /data/shared/Qwen/checkpoint/
        （未传 --backend → 默认 vllm 后端）
```

#### 4.5.3 源码精读

- 默认值在 [docker_web_demo.sh:L5-L8](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/docker_web_demo.sh#L5-L8)：镜像 `qwenllm/qwenvl:qwen3vl-cu128`、默认 checkpoint `Qwen/Qwen3-VL-235B-A22B-Instruct`、宿主端口 8901、容器名 qwen3vl。
- [docker_web_demo.sh:L16-L44](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/docker_web_demo.sh#L16-L44)：`while + case` 循环解析 `-i/--image-name`、`-c/--checkpoint`、`-n/--container-name`、`--port`、`-h/--help`，未知参数直接报错退出。
- [docker_web_demo.sh:L46-L49](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/docker_web_demo.sh#L46-L49)：**权重本地校验**——要求 checkpoint 路径下存在 `config.json`。注意这意味着不能传 HF Hub id（`Qwen/Qwen3-VL-2B-Instruct` 这种名字过不了检查），必须先把权重下载到本地目录。
- [docker_web_demo.sh:L56-L60](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/docker_web_demo.sh#L56-L60)：`docker run` 的完整参数——`--gpus all` 直通 GPU；`-d` 后台守护；`--restart always` 崩溃自动拉起；`-p ${PORT}:80` 把容器内 80 端口映射到宿主；`--mount type=bind` 把宿主权重目录原样挂到容器内 `/data/shared/Qwen/checkpoint`；容器内执行 `python web_demo_mm.py --server-port 80 --server-name 0.0.0.0 -c /data/shared/Qwen/checkpoint/`。**没有传 `--backend`**，因此走默认 vllm 后端（镜像里有 vllm，恰好自洽）。
- [docker_web_demo.sh:L60-L63](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/docker_web_demo.sh#L60-L63)：成功后提示用 `docker logs` 看状态、`docker rm -f` 停止删除。

#### 4.5.4 代码实践

1. **实践目标**：不启动容器也能吃透脚本——写出"本地 2B 权重 + 端口 8881"的完整命令，并核对镜像内容与依赖清单的差异。
2. **操作步骤**：
   1. 先下载权重到本地，例如 `huggingface-cli download Qwen/Qwen3-VL-2B-Instruct --local-dir ./Qwen3-VL-2B-Instruct`，确认该目录下有 `config.json`；
   2. 写出命令：`bash docker/docker_web_demo.sh -c ./Qwen3-VL-2B-Instruct --port 8881 -n qwen3vl-2b`；
   3. 对照 [Dockerfile-qwen3vl-cu128:L5-L11](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/docker/Dockerfile-qwen3vl-cu128#L5-L11) 与 [requirements_web_demo.txt](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/requirements_web_demo.txt)，手绘一张"镜像内包 vs requirements 包"对照表。
3. **需要观察的现象**：对照表里 vllm、qwen-vl-utils、torchcodec 三项只出现在镜像侧；transformers 的安装方式两侧不同（requirements 固定 commit `7aa888b`，镜像用 git 主分支）。
4. **预期结果**：能解释"为什么 Docker 方式默认 vllm 后端能跑、pip 方式默认后端跑不起来"——差的就是镜像侧预装的 vllm 与 qwen-vl-utils。实际启动需 GPU 机器与 nvidia-container-toolkit，待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么容器内要 `--server-name 0.0.0.0`，而本地直跑时默认 `127.0.0.1` 就够？
答案：容器有独立网络栈，`127.0.0.1` 只监听容器自己的回环地址，宿主机端口映射（`-p 8881:80`）转发进来的流量到不了它；必须 `0.0.0.0` 监听所有网卡，端口映射才生效。本地直跑时浏览器和服务在同一台机器，回环地址即可。

**练习 2**：`-c Qwen/Qwen3-VL-2B-Instruct` 传给 docker 脚本会怎样？
答案：L46 的检查会因 `Qwen/Qwen3-VL-2B-Instruct/config.json` 不存在而直接退出并打印 `Checkpoint config.json file not found ... exit.`。docker 脚本只接受**本地权重目录**，这是它与直接 `python web_demo_mm.py`（可接受 Hub id、自动下载）的重要区别。

**练习 3**：`--restart always` 与脚本结尾提示的 `docker rm -f` 各解决什么问题？
答案：`--restart always` 让容器在崩溃或 Docker 守护进程重启后自动拉起，适合长期挂着的演示服务；`docker rm -f` 强制停止并删除容器，是这套部署的"关停"手段（只 stop 不 rm 的话，`always` 策略下它还可能被再次拉起）。

## 5. 综合实践

把本讲四个模块串成一次完整操作，分两个任务（对应讲义规格中的实践任务）。

**任务 A：用 hf 后端启动并完成一次图片问答**

1. 实践目标：跑通"安装依赖 → 启动 → 上传图片 → 提问 → 观察流式输出"全流程。
2. 操作步骤：
   1. `pip install -r requirements_web_demo.txt`（flash-attn 保持注释状态，不装也能跑）；
   2. 启动（务必覆盖默认的 235B checkpoint）：

      ```bash
      python web_demo_mm.py \
          --backend hf \
          -c Qwen/Qwen3-VL-2B-Instruct \
          --server-name 127.0.0.1 --server-port 7860
      ```

   3. 浏览器打开终端里给出的 `http://127.0.0.1:7860`；
   4. 点 📁 Upload 上传一张图片 → 在 Input 框输入"描述这张图片" → 点 🚀 Submit；
   5. 不清空历史，接着追问一句与图片相关的后续问题（验证 4.3 的多轮重组）。
3. 需要观察的现象：回复逐段浮现（TextIteratorStreamer 生效）；终端打印 `User:` 与 `Qwen-VL-Chat:` 日志；页面标题显示 `Backend: HF`；第二轮回答能正确引用图片内容（说明历史里的 image 元素被重建进 messages）。
4. 预期结果：两个问题均得到与图片相关的回答。首次运行会下载约 4-5GB 权重，加载与推理速度取决于本机显卡。待本地验证。

**任务 B：阅读 _get_args 后写出 vllm 后端启动命令并记入笔记**

1. 实践目标：把 4.1 的参数表转化为一条可执行、可解释的命令。
2. 操作步骤：先补装 `pip install "vllm>=0.11.0" qwen-vl-utils`，然后写出并理解下面这条命令，逐参数标注含义后存入 `notes.md`：

   ```bash
   python web_demo_mm.py \
       --backend vllm \
       -c Qwen/Qwen3-VL-2B-Instruct \
       --tensor-parallel-size 1 \
       --gpu-memory-utilization 0.7 \
       --server-name 127.0.0.1 --server-port 7860
   ```

3. 需要观察的现象：vLLM 引擎初始化日志（显存占比、CUDA graph 捕获）远长于 hf 后端；随后的回答整段一次性出现（4.4 的伪流式）。
4. 预期结果：命令成功启动且页面标题显示 `Backend: VLLM`；`--tensor-parallel-size 1` 显式指定后，即使机器有多卡也不会自动并行。待本地验证。

## 6. 本讲小结

- `web_demo_mm.py` 的主干只有三步：`_get_args` 解析参数 → `_load_model_processor` 双后端加载 → `_launch_demo` 搭界面进入事件循环。
- 默认后端是 **vllm**、默认模型是 **235B-A22B**，而 `requirements_web_demo.txt` 不含 vllm/qwen-vl-utils——`try/except` 容错导入（L15-L21）让 hf 后端零额外依赖可跑，vllm 后端缺包时才在加载阶段抛出带安装提示的 ImportError。
- 参数分属两个世界：`--cpu-only`/`--flash-attn2` 只作用于 hf 分支，`--gpu-memory-utilization`/`--tensor-parallel-size` 只作用于 vllm 分支，`--tensor-parallel-size` 缺省时自动取 GPU 数。
- 会话重组的核心规则：文件按扩展名二分为 image/video 并以绝对路径入 content，文本收尾一条 user 消息，末尾 `messages.pop()` 弹掉空的 assistant 占位，最后 `_transform_messages` 把简写键规范成带 `type` 的标准 messages。
- 流式输出分真伪：hf 路径用 `TextIteratorStreamer` + 后台线程逐 token yield（真流式）；vllm 离线 `LLM.generate` 整批返回，单条提问只 yield 一次（伪流式），真流式要走 vLLM server。
- Docker 路线：脚本先校验**本地**权重目录的 `config.json`，再把权重 bind 挂载进基于 vllm-openai 的镜像、以默认 vllm 后端跑在容器 80 端口并映射到宿主。

## 7. 下一步学习建议

本讲是单元一的收官。你已经具备"让模型动起来"的完整经验：脚本推理（u1-l3）+ 网页交互（本讲）。接下来两条路：

- **单元二（u2-l1 起）**：`_prepare_inputs_for_vllm` 里一闪而过的 `process_vision_info`、`image_patch_size`、`return_video_metadata` 将成为主角——进入 qwen-vl-utils 的图像/视频预处理源码，理解像素预算与视觉 token 数的换算。
- **单元三（u3-l2、u3-l3）**：把本讲的"伪流式"升级为真流式——vLLM 离线批量推理的 `prepare_inputs_for_vllm` 模式与 `vllm serve` 在线服务的 OpenAI 兼容 API。

如果想在微调方向推进，也可以先跳到单元四（u4-l1 微调框架总览），但建议至少完成单元二的图像链路一讲再回去，因为微调数据侧同样依赖像素预算概念。
