# CLI 入口与包导出

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `tensorrt_edgellm` 安装后一共提供了哪六个 `tensorrt-edgellm-*` 命令，以及它们各自对应 `scripts/` 下的哪个 `main` 函数。
- 理解 `pyproject.toml` 里的 `[project.scripts]` 是如何把「一个字符串」变成终端里「一个可执行命令」的。
- 跟踪一条命令的完整执行链：从命令名 → 入口字符串 → `scripts/export.py` 的 `main()` → `argparse` → 实际的导出函数。
- 知道 `tensorrt_edgellm/__init__.py` 暴露了哪些公共 API（如 `AutoModel`、`export_onnx`），以及它们和命令行工具之间的关系。
- 写出一行最简单的「把一个检查点导出为 ONNX」的命令调用。

## 2. 前置知识

### 什么是「命令行入口（console script）」

你肯定用过 `pip`、`pytest`、`git` 这种在终端里直接敲名字就能运行的命令。它们本质上都只是磁盘上的一个小脚本文件，放在系统的 `PATH` 环境变量指向的目录里。

Python 的打包工具（如 `setuptools`）提供了一种标准机制，让你在**安装一个包的时候，自动生成一个可执行命令**。你只需要在 `pyproject.toml` 里写一行类似下面这样的配置：

```toml
[project.scripts]
my-command = "mypkg.mymodule:my_function"
```

这一行的含义是：

- 等号左边 `my-command` 是将来终端里的命令名。
- 等号右边 `"mypkg.mymodule:my_function"` 是一个**入口字符串**，冒号 `:` 前面是 Python 模块的导入路径，冒号后面是这个模块里的一个函数名。

当你执行 `pip install` 安装这个包时，`pip` 会读取这段配置，自动在你的 Python 环境的 `bin/`（Linux）或 `Scripts/`（Windows）目录下生成一个与命令同名的可执行文件。这个文件内部做的事情非常简单，等价于：

```python
from mypkg.mymodule import my_function
my_function()
```

> 关键点：**冒号后面的函数必须是「无参可调用」的**。命令行参数不是通过函数参数传进来的，而是由这个函数自己在内部用 `argparse` 去解析 `sys.argv`。本讲后面会反复看到这个模式。

### 入口字符串与 `argparse` 的配合

命令行入口函数通常长这样（伪代码）：

```python
import argparse

def main():
    parser = argparse.ArgumentParser(prog="my-command")
    parser.add_argument("model")          # 位置参数
    parser.add_argument("--dtype")        # 可选参数
    args = parser.parse_args()            # 解析 sys.argv
    do_real_work(args.model, args.dtype)  # 真正干活
```

`main()` 没有任何参数。`argparse` 会自动从 `sys.argv` 里读取用户在终端敲的参数。这也是为什么入口函数习惯命名为 `main`——它就是程序的「主入口」。

> 术语提示：`argparse` 是 Python 标准库里用来写命令行工具的模块；`sys.argv` 是一个列表，存放终端命令里用空格分隔的各个部分。

理解了上面这两点，本讲剩下的内容就是「照着源码读」了。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [`pyproject.toml`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/pyproject.toml) | Python 包的「总配置」：定义包名、依赖、可选依赖，以及**六个命令行入口** |
| [`tensorrt_edgellm/__init__.py`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/__init__.py) | Python 包的对外「门面」：定义 `__all__` 暴露的公共 API，并在导入时注册各模型类 |
| [`tensorrt_edgellm/scripts/export.py`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py) | `tensorrt-edgellm-export` 命令的真正实现，本讲用来做「跟踪一条命令」的样本 |
| [`tensorrt_edgellm/scripts/quantize.py`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py) | `tensorrt-edgellm-quantize` 命令的实现，用来对比「带子命令」的 CLI 模式 |
| [`AGENTS.md`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/AGENTS.md) | 项目维护者手册，其中有一张「CLI 入口」速查表 |

> 一个容易混淆的点：`scripts/` 目录里**有 7 个 `.py` 文件**，但只有 **6 个**被注册成了命令（`sweep_eagle3_configs.py` 没有注册）。这说明「有 `main()` 的脚本」和「对外暴露的命令」不是一回事——必须出现在 `[project.scripts]` 里才会变成命令。我们稍后会用到这个判断。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 CLI 脚本映射**：搞清楚 `[project.scripts]` 如何把命令映射到 `main` 函数，并列出六张对照表。
- **4.2 跟踪一条命令：export 的执行链**：深入 `scripts/export.py`，看一个命令从被敲下到真正干活的全过程。
- **4.3 Python 包导出**：理解 `__init__.py` 暴露的公共 API，以及它和命令行工具的关系。

### 4.1 CLI 脚本映射

#### 4.1.1 概念说明

`tensorrt_edgellm` 是一个 Python 包。当你在开发机（通常是 x86）上 `pip install` 它之后，除了能在代码里 `import tensorrt_edgellm` 之外，还会得到一组可以直接在终端敲的命令。这些命令覆盖了导出流水线里所有「人工需要触发」的环节：

- **导出**：把检查点变成 ONNX。
- **量化**：把检查点变成更省内存的量化检查点。
- **LoRA**：往 ONNX 图里插入 / 处理 / 合并 LoRA 适配器。
- **词表裁剪**：生成更小的词表映射，减少输出层体积。

这套命令的好处是：**用户不需要写 Python 脚本**，只要敲一条命令、传几个参数，就能完成流水线里的一步。而它们背后的实现，全部集中在 `tensorrt_edgellm/scripts/` 目录下，每个命令对应一个文件、一个 `main` 函数。

#### 4.1.2 核心流程

一条命令从「用户在终端敲下」到「真正执行」的过程：

1. 用户敲 `tensorrt-edgellm-export /path/to/ckpt /tmp/out`。
2. 操作系统在 `PATH` 里找到名为 `tensorrt-edgellm-export` 的可执行文件（它是 `pip install` 时根据 `[project.scripts]` 自动生成的）。
3. 这个可执行文件执行等价于 `from tensorrt_edgellm.scripts.export import main; main()` 的代码。
4. `main()` 内部用 `argparse` 解析 `/path/to/ckpt /tmp/out`，拿到参数。
5. `main()` 调用真正的业务函数（如 `_export_llm`），后者再调用底层 API `export_onnx`。

```
终端命令 ──► [project.scripts] 入口字符串 ──► scripts/xxx.py 的 main()
                                                    │
                                              argparse 解析参数
                                                    │
                                          调用业务函数 (e.g. _export_llm)
                                                    │
                                         调用底层 API (e.g. export_onnx)
```

#### 4.1.3 源码精读

先看总开关 [`pyproject.toml:54-60`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/pyproject.toml#L54-L60)，这里就是六个命令的定义，每行一条：

```toml
[project.scripts]
tensorrt-edgellm-quantize   = "tensorrt_edgellm.scripts.quantize:main"
tensorrt-edgellm-export     = "tensorrt_edgellm.scripts.export:main"
tensorrt-edgellm-insert-lora  = "tensorrt_edgellm.scripts.insert_lora:main"
tensorrt-edgellm-process-lora = "tensorrt_edgellm.scripts.process_lora_weights:main"
tensorrt-edgellm-merge-lora   = "tensorrt_edgellm.scripts.merge_lora:main"
tensorrt-edgellm-reduce-vocab = "tensorrt_edgellm.scripts.reduce_vocab:main"
```

读法（以 `export` 那行为例）：

- 命令名：`tensorrt-edgellm-export`
- 模块路径：`tensorrt_edgellm.scripts.export`（即 `tensorrt_edgellm/scripts/export.py`）
- 函数名：`main`

注意两点细节：

1. **命令名用连字符 `-`，模块路径用下划线 `_`**。这是 Python 生态的惯例：可执行命令名可以用连字符（更易读），但 Python 标识符和包/模块名不允许连字符，只能用下划线。
2. **`process-lora` 对应的文件名是 `process_lora_weights.py`**（不是 `process_lora.py`），命令名和文件名并不严格一一对应——以冒号右边的模块路径为准。

把六条规则整理成对照表（这正是 `AGENTS.md` 里也维护的速查表，见 [`AGENTS.md:65-74`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/AGENTS.md#L65-L74)）：

| 命令 | 入口模块 | `main` 所在文件 | 用途 |
|------|----------|-----------------|------|
| `tensorrt-edgellm-export` | `scripts.export` | `export.py` | 检查点 → ONNX |
| `tensorrt-edgellm-quantize` | `scripts.quantize` | `quantize.py` | 检查点 → 量化检查点 |
| `tensorrt-edgellm-insert-lora` | `scripts.insert_lora` | `insert_lora.py` | 往 ONNX 图里插入 LoRA 钩子 |
| `tensorrt-edgellm-process-lora` | `scripts.process_lora_weights` | `process_lora_weights.py` | 转换 LoRA adapter 权重格式 |
| `tensorrt-edgellm-merge-lora` | `scripts.merge_lora` | `merge_lora.py` | 把 LoRA 静态合并进权重 |
| `tensorrt-edgellm-reduce-vocab` | `scripts.reduce_vocab` | `reduce_vocab.py` | 生成裁剪词表的映射 |

每个入口函数都确实存在。可以用一条命令快速验证（见 [`Grep`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts) 目录）：六个文件里各自有一个 `def main()`。

最后注意一个**反面例子**：`scripts/sweep_eagle3_configs.py` 里也有一个 `main()`（见 [`scripts/sweep_eagle3_configs.py:505`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/sweep_eagle3_configs.py#L505)），但它**没有**出现在 `[project.scripts]` 里，所以安装后不会生成对应命令。它只能用 `python -m tensorrt_edgellm.scripts.sweep_eagle3_configs` 这种方式手动调用。这印证了前面的结论：**是否成为命令，完全由 `[project.scripts]` 决定，与文件里有没有 `main()` 无关。**

#### 4.1.4 代码实践

> 实践目标：亲手验证「命令 ↔ 入口字符串 ↔ `main()`」三者的一一对应，建立对映射机制的直观信心。

操作步骤：

1. 打开 [`pyproject.toml`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/pyproject.toml#L54-L60) 的 `[project.scripts]` 段。
2. 任意挑一条，比如 `tensorrt-edgellm-reduce-vocab = "tensorrt_edgellm.scripts.reduce_vocab:main"`。
3. 根据冒号左边的模块路径，定位到 [`tensorrt_edgellm/scripts/reduce_vocab.py:33`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/reduce_vocab.py#L33)，确认那里确实有一个 `def main() -> None:`。
4. 对全部六条命令重复一次，确认六张映射都成立。

需要观察的现象：每个冒号右边的 `模块:函数`，都能在仓库里找到一个真实存在的 `def 函数(...)`。

预期结果：六条全部一一对应，没有悬空入口。同时你会发现 `sweep_eagle3_configs.py` 虽有 `main()`，却不在列表里。

> 说明：本实践是纯源码阅读型，不需要 GPU 或模型，可在任何能访问源码的环境完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么命令名用连字符（`tensorrt-edgellm-export`）而模块名用下划线（`scripts.export`）？

**参考答案**：命令名是操作系统层面的可执行文件名，连字符合法且更易读；而 Python 的包/模块名是标识符，不允许出现连字符，必须用下划线。两者分别服务于「终端命令」和「Python 导入」两个不同世界。

**练习 2**：`tensorrt-edgellm-process-lora` 对应的文件叫什么？为什么不能想当然地猜成 `process_lora.py`？

**参考答案**：对应 `process_lora_weights.py`。命令名只是给人看的标签，真正的依据是入口字符串 `tensorrt_edgellm.scripts.process_lora_weights` 里冒号左边的模块路径，它才是被 `import` 的对象。

---

### 4.2 跟踪一条命令：export 的执行链

上一模块讲了「映射表」，这一模块挑 `tensorrt-edgellm-export` 这一条命令，完整走一遍它的内部执行链，让你看到一个命令「真正怎么干活」。这也为下一讲（u1-l5 端到端流水线实战）铺路。

#### 4.2.1 概念说明

`export` 命令是整条流水线的起点：它读入一个 HuggingFace 检查点目录，输出一组 ONNX 子图（以及一些「侧车」sidecar 权重文件）。它的设计有两个特点：

- **它是「多模态总指挥」**：一次调用可能导出多个组件——LLM 主干、视觉编码器、音频编码器、声码器（code2wav）、以及投机解码用的 draft 模型。具体导出哪些，由检查点的 `config.json` 里的 `model_type` 自动决定。
- **它的命令名和内部 argparse 的 `prog` 一致**：`main()` 里显式写了 `prog="tensorrt-edgellm-export"`，保证 `--help` 输出的命令名和终端实际敲的命令名吻合。

#### 4.2.2 核心流程

`scripts/export.py` 的 `main()` 大致经过下面几个阶段：

1. **设置进程级 umask**：保证导出的 ONNX 文件权限是 `0o644`（其他用户可读），避免容器内不同用户间读不了文件。
2. **用 argparse 解析参数**：最关键的两个是位置参数 `model`（检查点路径）和 `output_dir`（输出根目录），外加一堆 `--skip-*`、`--dtype` 等开关。
3. **从 `config.json` 推断 model_type**，据此判断该检查点有哪些组件（视觉？音频？draft？）。
4. **构建一张「阶段表」`stages`**：每个元素是一个三元组 `(是否启用, 组件名, 导出函数)`。
5. **遍历 `stages`**：对每个启用的组件，调用它的导出函数，把输出子目录传进去。
6. **打印汇总**：列出每个组件生成的 `model.onnx` 及其 sidecar 文件的大小。

每个具体的导出函数（如 `_export_llm`、`_export_visual`）最后都会调用同一个底层 API `export_onnx` 来真正产出 ONNX。

#### 4.2.3 源码精读

入口函数本身在 [`scripts/export.py:2373`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2373)。注意它的签名——**没有参数**，印证了「命令行入口函数是无参的」这一规则：

```python
def main() -> None:
    os.umask(0o022)                 # ① 文件权限
    p = argparse.ArgumentParser(
        prog="tensorrt-edgellm-export",   # ② 命令名与 [project.scripts] 对齐
        description=("Export ALL components of a multimodal checkpoint to ONNX ..."))
    p.add_argument("model", ...)          # 位置参数：检查点
    p.add_argument("output_dir", ...)     # 位置参数：输出目录
    p.add_argument("--dtype", default="float16", ...)
    p.add_argument("--skip-llm", action="store_true", ...)
    ...
```

参数定义见 [`scripts/export.py:2382-2401`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2382-L2401)。

核心的「阶段表」构建在 [`scripts/export.py:2712-2769`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2712-L2769)，每个元素是 `(enabled, component, callable)`：

```python
stages = [
    (_has_llm_component(model_type, "thinker") and not args.skip_llm ...,
     "thinker",  lambda out: _export_llm(model_dir, out, model_type=model_type, ...)),
    ...
    (_has_visual(model_type) and not args.skip_visual ...,
     "visual",  _export_visual_component),
    (_has_audio(model_type) and not args.skip_audio ...,
     "audio",   lambda out: _export_audio(...)),
    ...
]
```

真正「派活」的循环非常简洁，见 [`scripts/export.py:2810-2814`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2810-L2814)：

```python
for enabled, component, fn in stages:
    if enabled:
        fn(os.path.join(args.output_dir, _layout_for(model_type, component)))
```

也就是说：对每个启用的组件，调用它的函数 `fn`，并把「输出根目录 + 该组件的子路径」作为参数传进去。

最后，每个 `_export_*` 函数（以 `_export_llm` 为例，见 [`scripts/export.py:866-899`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L866-L899)）内部都会走到同一个底层 API：

```python
from ..model import AutoModel
model = AutoModel.from_pretrained(model_dir, ...)
...
from ..onnx.export import export_onnx
export_onnx(model, output_path, model_dir=model_dir, ...)
```

这条链的终点 `export_onnx` 正是 `__init__.py` 暴露的公共 API 之一（见 4.3）。也就是说：**命令行工具和 Python 公共 API，底层调用的是同一套实现**，只是入口不同。

文件末尾还有标准的「直接运行」守护，见 [`scripts/export.py:2859-2860`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2859-L2860)：

```python
if __name__ == "__main__":
    main()
```

> 对比：`quantize` 命令用的是另一种 CLI 模式——**带子命令**（`llm` / `draft` / `qwen3-omni`），见 [`scripts/quantize.py:119-167`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L119-L167)，用 `add_subparsers` 实现。`export` 则是「扁平」参数。两种都是 argparse 的常见用法。

#### 4.2.4 代码实践

> 实践目标：根据本模块的源码追踪，写出一行最简单的 `export` 命令调用，并解释它的每个部分。

**操作步骤**：

1. 回顾入口字符串：`tensorrt-edgellm-export = "tensorrt_edgellm.scripts.export:main"`。
2. 回顾 `main()` 的两个位置参数：`model`（检查点）和 `output_dir`（输出目录）。
3. 因此最简调用只需要两个位置参数。

最简命令（参照文件头部 docstring 的示例 [`scripts/export.py:24-27`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L24-L27)）：

```bash
# 最简形式：检查点目录 + 输出目录
tensorrt-edgellm-export /path/to/checkpoint /tmp/onnx_out
```

如果检查点还没下载到本地，也可以直接传 HuggingFace 模型 ID，`export.py` 会尝试用 `huggingface_hub` 拉取（见 [`scripts/export.py:242-252`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L242-L252) 的 `_resolve_model_dir`）：

```bash
# 用 HF 模型 ID（会自动下载）
tensorrt-edgellm-export Qwen/Qwen2.5-0.5B /tmp/onnx_out
```

**需要观察的现象**（待本地验证，需要 GPU + 已安装 `[tools]` 依赖）：

- 终端会先打印一条 `============================` 分隔线，列出该 model_type 检测到的各组件是否启用（`yes/no`）。
- 然后逐组件打印 `[LLM] Loading checkpoint ...`、`[LLM] Exporting to ...`。
- 最后打印 `Export complete` 汇总，列出每个组件的 `model.onnx` 大小。

**预期结果**：在 `/tmp/onnx_out/llm/` 下生成 `model.onnx`（以及 `config.json` 和可能的 sidecar 文件）。

> 说明：本命令的真正运行需要 GPU、已量化的检查点或原始检查点、以及 `pip install ".[tools]"` 装好的依赖。若本地不具备，至少应能组装出完整命令并解释：第一个位置参数是检查点，第二个是输出目录，`--dtype` 默认 `float16`。

#### 4.2.5 小练习与答案

**练习 1**：`main()` 函数没有任何参数，那用户在终端敲的 `/tmp/onnx_out` 是怎么传进程序的？

**参考答案**：通过 `sys.argv`。`argparse.ArgumentParser().parse_args()` 默认读取 `sys.argv[1:]`，把位置参数 `model`、`output_dir` 绑定到对应位置。入口函数本身不需要形参。

**练习 2**：为什么 `export.py` 要在 `main()` 开头调用 `os.umask(0o022)`？

**参考答案**：ONNX 库在某些代码路径里用 `open(path, 'wb')` 创建外部数据文件，会套用进程 umask。容器镜像常带严格 umask（如 `0o077`），导致生成的 ONNX 文件权限是 `0o600`，下游构建引擎的机器若以别的用户挂载该目录就读不了。固定 umask 为 `0o022` 可保证产物对其他用户可读。

**练习 3**：`export`（扁平参数）和 `quantize`（带 `llm`/`draft`/`qwen3-omni` 子命令）在 argparse 写法上的本质区别是什么？

**参考答案**：`export` 直接用 `add_argument` 添加位置参数和开关；`quantize` 先用 `add_subparsers(dest="command", required=True)` 建一组子命令，再给每个子命令各自 `add_argument`。子命令模式适合「一个工具干多种性质不同的事」，扁平模式适合「一个工具干一件事、只是有很多选项」。

---

### 4.3 Python 包导出

CLI 命令是给「终端用户」用的；而 `__init__.py` 暴露的公共 API，是给「写 Python 代码的人」用的。两者底层共用同一套实现。

#### 4.3.1 概念说明

当你 `import tensorrt_edgellm` 时，Python 会执行 `tensorrt_edgellm/__init__.py` 里的所有顶层语句。这个文件承担两个职责：

1. **决定「对外暴露什么」**：通过 `__all__` 列表声明包的公共 API。只有出现在 `__all__` 里的名字，才被认为是「稳定的、给外部用的」接口。
2. **执行「导入时的副作用」**：在导入时调用 `register_model(...)` 把一批模型类注册进全局注册表，这样后续 `AutoModel.from_pretrained(...)` 才能根据 `model_type` 找到对应实现。

> 术语提示：`__all__` 是 Python 的一个约定。它是一个字符串列表，告诉别人「`from tensorrt_edgellm import *` 应该导入哪些名字」，也相当于一份「公开 API 清单」。

#### 4.3.2 核心流程

`__init__.py` 的执行流程：

1. 从各子模块 `import` 一批符号（如 `AutoModel`、`export_onnx`、`ModelConfig`）。
2. 用 `register_model(...)` 注册约二十个 `model_type → 模型类` 映射，并注册对应的 attention scale 函数。
3. 定义 `__all__`，圈定对外暴露的 9 个公共符号。

对外用户有两种等价的用法：

- **命令行**：`tensorrt-edgellm-export ...`（适合一次性操作）。
- **Python API**：`from tensorrt_edgellm import AutoModel, export_onnx`（适合写脚本、做定制）。

两者最终都调用同一个 `export_onnx`。

#### 4.3.3 源码精读

文件顶部的 docstring 直接给出了 Python API 的「速查示例」，见 [`tensorrt_edgellm/__init__.py:21-26`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/__init__.py#L21-L26)：

```python
from tensorrt_edgellm import AutoModel, export_onnx

model = AutoModel.from_pretrained("/path/to/checkpoint")
export_onnx(model, "output/model.onnx", model_dir="/path/to/checkpoint")
```

这就是 `export` 命令在 Python 层的等价写法——三行代码完成「加载检查点 + 导出 ONNX」。

接着是一长串导入语句（见 [`__init__.py:33-52`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/__init__.py#L33-L52)），把分散在各子模块的能力「收口」到包的顶层。关键几行：

```python
from ._version import __version__
from .checkpoint.loader import load_weights
from .config import ModelConfig, QuantConfig
from .model import (AutoModel, register_attention_scale_default,
                    register_model, standard_attention_scale)
...
from .onnx.export import export_onnx
```

注意这里也导入了各 `models/*` 的具体模型类（如 `Qwen3MoeCausalLM`、`NemotronHCausalLM`），目的不是暴露给用户，而是为了让紧接着的 `register_model` 能引用它们——见 [`__init__.py:60-91`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/__init__.py#L60-L91)：

```python
register_model("nemotron_h", NemotronHCausalLM, standard_attention_scale)
register_model("qwen3_5_text", Qwen3_5CausalLM, standard_attention_scale)
register_model("qwen3_moe", Qwen3MoeCausalLM, standard_attention_scale)
...
```

每个 `register_model("model_type", SomeClass, scale_fn)` 的含义是：「当检查点的 `config.json` 里 `model_type` 等于这个字符串时，用 `SomeClass` 来构建模型」。这些注册是「导入时副作用」——`import tensorrt_edgellm` 这一句执行完，注册表就已经填好了。（注册机制与分发逻辑本身是 u2-l2 的主题，本讲只需知道「注册发生在 `__init__.py`」即可。）

最后是公共 API 清单，见 [`__init__.py:93-103`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/__init__.py#L93-L103)：

```python
__all__ = [
    "__version__",
    "AutoModel",
    "export_onnx",
    "load_checkpoint_config_dicts",
    "load_config_dict",
    "load_weights",
    "ModelConfig",
    "QuantConfig",
    "register_model",
]
```

这 9 个就是包对外承诺的「稳定 API」。它们的职责一览：

| 公共 API | 来自 | 作用 |
|----------|------|------|
| `AutoModel` | `model.py` | 按检查点的 `model_type` 自动选择并构造模型（`from_pretrained`） |
| `export_onnx` | `onnx/export.py` | 把模型导出为 EdgeLLM 可接受的 ONNX |
| `ModelConfig` / `QuantConfig` | `config.py` | 解析检查点的架构字段与量化元数据 |
| `load_checkpoint_config_dicts` / `load_config_dict` | `checkpoint/checkpoint_utils.py` | 读取并合并 `config.json` / `hf_quant_config.json` |
| `load_weights` | `checkpoint/loader.py` | 从 safetensors 加载权重 |
| `register_model` | `model.py` | 把新模型类注册进全局注册表（扩展点） |
| `__version__` | `_version.py` | 包版本号（也驱动 `pyproject.toml` 的 `dynamic = ["version"]`，见 [`pyproject.toml:73-74`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/pyproject.toml#L73-L74)） |

> 顺带一提：`pyproject.toml` 的 `[tool.setuptools.packages.find]` 同时包含了 `tensorrt_edgellm*` 和 `experimental*`（见 [`pyproject.toml:65-67`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/pyproject.toml#L65-L67)），所以安装时也会把实验性的服务端包一并装上。这部分是 u9-l5 的主题。

#### 4.3.4 代码实践

> 实践目标：用 Python 公共 API 复现 `export` 命令的最小行为，体会「命令行与 API 底层同源」。

**操作步骤**：

1. 在交互式 Python 或脚本里执行 docstring 给出的三行示例。
2. 对照 4.2 里 `export` 命令产生的产物，体会两者等价。

```python
# 示例代码（非项目原有，仿照 __init__.py docstring 编写）
from tensorrt_edgellm import AutoModel, export_onnx

model = AutoModel.from_pretrained("/path/to/checkpoint")
export_onnx(model, "output/model.onnx", model_dir="/path/to/checkpoint")
```

**需要观察的现象**（待本地验证）：`AutoModel.from_pretrained` 会打印出它根据 `config.json` 选中的模型类；`export_onnx` 会在 `output/` 下生成 `model.onnx`。

**预期结果**：得到与 `tensorrt-edgellm-export /path/to/checkpoint output/`（仅 LLM 组件时）基本一致的 `model.onnx`。

> 说明：此示例代码仅用于说明 API 等价性，真正运行同样需要 GPU 与检查点。若仅阅读源码，可重点体会：`export` 命令的 `_export_llm` 内部正是调用了这两个 API（见 4.2.3 的源码片段）。

#### 4.3.5 小练习与答案

**练习 1**：`__init__.py` 里导入了很多 `models/*` 的具体类（如 `Qwen3MoeCausalLM`），但它们大多**没有**出现在 `__all__` 里。为什么还要导入它们？

**参考答案**：导入它们是为了让紧随其后的 `register_model("qwen3_moe", Qwen3MoeCausalLM, ...)` 能引用到这些类，完成「导入时注册」。它们是注册表的「材料」，不是给用户的公开 API，所以不放进 `__all__`。

**练习 2**：`register_model` 既出现在 `__init__.py` 的导入语句里，又出现在 `__all__` 里。这两处分别意味着什么？

**参考答案**：导入语句里的 `register_model` 是为了让 `__init__.py` 自己能在注册阶段调用它；`__all__` 里的 `register_model` 则表示它对外公开——外部开发者可以用它注册自己的模型类（这正是 u9-l4「接入新模型架构」会用到的一个扩展点）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个小任务：**为「接入一个新的检查点」做一次命令行演练**。

任务背景：假设你刚拿到一个本地的 LLM 检查点目录 `/data/my_model`，你想把它变成 ONNX。

要求：

1. **找命令**：在 [`pyproject.toml:54-60`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/pyproject.toml#L54-L60) 里指出你会用哪一个命令，并写出它对应的入口字符串和 `main` 函数所在文件。
2. **查依赖**：参考 [`pyproject.toml:24-51`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/pyproject.toml#L24-L51)，判断仅用基础依赖能否运行 `export`？（提示：`export` 的核心路径只依赖基础依赖；但若检查点需要量化等，则需要 `pip install ".[tools]"`。）
3. **组装命令**：写出最简导出命令，并标注哪一段是检查点、哪一段是输出目录。
4. **换成 API**：再写一段等价的 Python 代码（用 `__init__.py` 暴露的公共 API）完成同样的事。

参考答案要点：

1. 用 `tensorrt-edgellm-export`，入口字符串 `tensorrt_edgellm.scripts.export:main`，`main` 在 `tensorrt_edgellm/scripts/export.py:2373`。
2. `export` 的主干只用到基础依赖（`torch`/`transformers`/`onnx`/`safetensors` 等）；但若涉及词表裁剪、LoRA、量化，则需要 `[tools]`（含 `nvidia-modelopt`、`datasets`、`peft` 等）。
3. `tensorrt-edgellm-export /data/my_model /tmp/onnx_out`（前者检查点，后者输出目录）。
4.
   ```python
   from tensorrt_edgellm import AutoModel, export_onnx
   model = AutoModel.from_pretrained("/data/my_model")
   export_onnx(model, "/tmp/onnx_out/model.onnx", model_dir="/data/my_model")
   ```

## 6. 本讲小结

- `tensorrt_edgellm` 安装后提供 **6 个** `tensorrt-edgellm-*` 命令，全部由 `pyproject.toml` 的 `[project.scripts]` 定义；命令名用连字符，模块路径用下划线。
- 入口字符串的格式是 `"模块路径:函数名"`，函数必须无参，命令行参数靠 `main()` 内部的 `argparse` 解析 `sys.argv`。
- 六个命令分别对应 `scripts/` 下的 `export.py`、`quantize.py`、`insert_lora.py`、`process_lora_weights.py`、`merge_lora.py`、`reduce_vocab.py`，每个都有一个 `def main()`。
- 以 `export` 为样本，一条命令的执行链是：入口字符串 → `main()` → argparse → 构建阶段表 `stages` → 遍历调用各 `_export_*` → 底层统一走 `export_onnx`。
- `__init__.py` 通过 `__all__` 暴露 9 个公共 API（`AutoModel`、`export_onnx`、`ModelConfig`/`QuantConfig`、`load_weights` 等），并在导入时用 `register_model` 填充模型注册表。
- **命令行工具和 Python 公共 API 底层同源**——它们最终都调用同一个 `export_onnx`，只是入口不同。

## 7. 下一步学习建议

- 下一讲 **u1-l5 端到端流水线实战** 会把你本讲认识的 `export` 命令真正跑起来，并接上 `llm_build` 与 `llm_inference` 两步，完成「检查点 → ONNX → engine → 推理」的闭环。
- 如果你对 `AutoModel` 如何根据 `model_type` 选模型类感到好奇，可以先跳读 [`tensorrt_edgellm/model.py`](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py)，那是 u2-l2「AutoModel 分发与模型注册表」的主题。
- 想先建立全局视图的读者，建议回头确认 u1-l2 讲的三段式流水线：本讲的命令属于其中的「Python 导出」一段。
```
