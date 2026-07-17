# 虚拟环境与依赖管理 venv.py

## 1. 本讲目标

前面几讲我们走完了「模型名 → `BentoInfo`」的解析（[u2-l4](u2-l4-model-discovery.md)），也掌握了把命令交给子进程执行的统一通道 `run_command`（[u2-l2](u2-l2-common-subprocess.md)）。但真正要 `bentoml serve` 一个模型时，还差最后一块拼图：**这个模型依赖的 Python 包（vLLM、transformers、torch……）装在哪里？谁来装？**

OpenLLM 本身是「编排者」，它并不自带这些沉重的推理依赖。它的选择是：**为每个 Bento 在 `~/.openllm/venv` 下单独建一个虚拟环境，用 `uv` 把依赖装进去，再让 `bentoml serve` 在这个环境里运行。** 本讲就聚焦 `src/openllm/venv.py` 这 100 行代码，讲清楚这套「按需建环境、按哈希缓存复用」的机制。

读完本讲，你应当能够：

- 说清楚 `VenvSpec` 如何把 `python_version / requirements / envs` 归一化，并用一个哈希值作为虚拟环境目录名，从而实现「同一份依赖只装一次」。
- 看懂 `_resolve_bento_venv_spec` 如何从 Bento 目录的 `requirements.lock.txt` / `requirements.txt` 与 `bento.yaml` 读出环境规格。
- 看懂 `_ensure_venv` 用 `uv venv` + `uv pip install` 创建环境、安装 `bentoml` 与依赖的完整流程，以及 `DONE` 标记、半成品清理、失败回滚的设计。

## 2. 前置知识

本讲承接以下已建立的认知（不再重复细节）：

- **`EnvVars` 是可哈希的环境变量容器**：继承 `UserDict`，构造时去空值、按 key 排序，提供确定性 `__hash__`。它是 venv 缓存复用的基石（见 [u2-l2](u2-l2-common-subprocess.md)）。
- **`run_command` 会重写命令**：`cmd[0] == 'python'` 时会被替换成当前解释器路径 `py`（默认 `sys.executable`），所以 `['python', '-m', 'uv', ...]` 实际跑的是 `<当前 python> -m uv ...`；失败时统一 `typer.Exit(1)`（见 [u2-l2](u2-l2-common-subprocess.md)）。
- **`BentoInfo` 指向磁盘上的 Bento 目录**：`bento.path` 是 `bento.yaml` 所在路径，`bento.tag` 是用户面向的 `name:version`（见 [u2-l4](u2-l4-model-discovery.md)）。
- **`VERBOSE_LEVEL` 是栈式 `ContextVar`**：默认 0，`--verbose` 会把它设为 20；`output(content, level=N)` 在 `level > VERBOSE_LEVEL.get()` 时静默（见 [u2-l1](u2-l1-common-config-output.md)）。
- **`VENV_DIR = OPENLLM_HOME / 'venv'`**：这个目录在 `import openllm.common` 时就会被创建（见 [u1-l2](u1-l2-install-and-layout.md)）。

一个直觉：你可以把本讲理解成一个**带缓存的构建器**——输入是「这个 Bento 需要什么环境」，输出是「一个已经装好依赖的虚拟环境目录路径」。缓存键不是模型名，而是「依赖内容本身的哈希」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/openllm/venv.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py) | 本讲主角。把 `BentoInfo` 变成一个装好依赖的虚拟环境目录。 |
| [src/openllm/common.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py) | 提供 `VenvSpec`（环境规格模型）、`EnvVars`、`VENV_DIR`、`run_command`、`output`、`VERBOSE_LEVEL` 等基础设施。 |
| [src/openllm/local.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py) | 调用方。`serve` / `_run_model` 在拼好命令后，都会先 `ensure_venv(bento, runtime_envs=env)` 拿到 venv，再带着这个 venv 去跑 `bentoml serve`。 |

`venv.py` 一共只有 5 个对外的函数，本讲按「规格 → 创建安装 → 缓存与清理」的顺序依次拆开：

```
ensure_venv(bento, runtime_envs)          # 唯一公开入口（local.py 调它）
   └─ _resolve_bento_venv_spec(bento, …)  # 读 lock/requirements + bento.yaml → VenvSpec（带 lru_cache）
   └─ _ensure_venv(venv_spec)             # 按 hash 取目录；不存在则 uv 建环境装依赖；写 DONE
check_venv(bento)                          # 只判断「是否已装好」，不触发安装（轻量探测）
```

## 4. 核心概念与源码讲解

### 4.1 VenvSpec：环境规格与归一化哈希

#### 4.1.1 概念说明

要给一个 Bento 建虚拟环境，需要回答三个问题：

1. **用哪个 Python 版本？**（`python_version`）
2. **装哪些包？**（`requirements`）
3. **装的时候需要哪些环境变量？**（`envs`，比如 `HF_TOKEN`）

这三样东西合起来，就是一份「环境规格」。OpenLLM 用 `VenvSpec` 这个 Pydantic 模型来表示它。但规格本身还不能直接当缓存键——同一个 Bento 的 `requirements.txt` 里，行的顺序可能变、可能有空行和注释，这些都不应该影响「这是不是同一份依赖」。

所以 `VenvSpec` 还做了一件关键的事：**归一化（normalize）**——把依赖列表排序、去掉注释和空行，算出一个确定的哈希。这个哈希就是虚拟环境目录的名字，也是「缓存命中」的依据。

#### 4.1.2 核心流程

`VenvSpec` 的哈希计算流程可以概括为：

```
requirements_txt（原始，可能有空行/注释/乱序）
        │
        ▼  normalized_requirements_txt（cached_property）
   分三类：参数行(-开头) / 依赖行 / 注释行(#开头)
   参数行排序 + 依赖行排序，拼接（注释行被丢弃）
        │
envs（EnvVars，已去空、已排序）
        │
        ▼  normalized_envs（cached_property）
   拼成字符串
        │
        ▼  __hash__ = md5(normalized_requirements_txt, str(hash(normalized_envs)))
   一个整数哈希 → 作为 venv 目录名
```

这里用到了一个工具函数 `md5`：它把任意多个字符串拼接后做 MD5，再转成整数。用 MD5 而不是内置 `hash()` 的好处是**跨进程稳定**——内置 `hash(str)` 受 `PYTHONHASHSEED` 影响，每次启动 Python 都可能不同，而 venv 目录名必须在「这次运行」和「下次运行」之间一致。

\[ \text{目录名} = \mathrm{int}(\text{MD5}(\text{归一化依赖} \,\Vert\, \text{hash}(\text{归一化envs})),\, 16) \]

#### 4.1.3 源码精读

`VenvSpec` 定义在 `common.py`，字段只有四个：

[common.py:L258-L262](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L258-L262) —— `python_version`、`requirements_txt`（原始文本）、`envs`（一个 `EnvVars`）、`name_prefix`（仅用于 `uv venv` 给环境起名，**不参与哈希**）。

依赖归一化的核心在 `normalized_requirements_txt`，它把每一行分到三个桶里：

[common.py:L264-L282](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L264-L282) —— 关键点有三：

- **空行直接跳过**（`if not line.strip(): continue`）；
- **注释行（`#` 开头）被收进 `comment_lines` 但最终没有拼接进返回值**——也就是说注释不影响哈希；
- **参数行（`-` 开头，如 `-e .`、`--index-url ...`）和普通依赖行分别排序**后再拼接（`parameter_lines + dependency_lines`）。

这样，「行序不同」「多了空行/注释」的等价依赖都会归一到同一个字符串。

`normalized_envs` 则把环境变量拼成字符串：

[common.py:L284-L286](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L284-L286)。

最后是哈希函数本身：

[common.py:L288-L290](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L288-L290) —— 只把「归一化依赖」和「归一化 envs 的哈希」喂给 `md5`。

它依赖的 `md5` 工具函数：

[common.py:L442-L446](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L442-L446) —— 逐个字符串 `update` 进 MD5 摘要，最后把十六进制摘要转成整数返回。

> **值得注意的细节（建议自行验证）**：`normalized_envs` 的过滤条件是 `if not v`，即只保留「值为空」的条目；而 `EnvVars` 在构造时已经把空值都剔除了（见 [u2-l2](u2-l2-common-subprocess.md)）。两者叠加的后果是：对一个合法的 `EnvVars`，`normalized_envs` 几乎总是空字符串 `''`。也就是说，**在常规路径下 `envs` 实际上几乎不影响哈希**，真正决定目录名的是 `normalized_requirements_txt`。此外，`python_version` 和 `name_prefix` 都没有进入哈希。这是源码当前的真实行为，本讲的实践任务会带你确认这一点（标为「待本地验证」）。

#### 4.1.4 代码实践

**实践目标**：直观感受「归一化让等价依赖命中同一哈希」。

**操作步骤**：

把下面这段保存为 `play_venvspec.py`（示例代码，非项目原有文件）：

```python
# 文件名: play_venvspec.py
from openllm.common import VenvSpec, EnvVars

# 同样的三个依赖，顺序不同，还混了空行和注释
raw_a = "\n".join([
    "# 这是一个注释，不应该影响哈希",
    "",
    "bentoml==1.2",
    "vllm==0.6",
    "torch==2.3",
])
raw_b = "\n".join([
    "torch==2.3",
    "vllm==0.6",
    "bentoml==1.2",
])

a = VenvSpec(python_version='3.11', requirements_txt=raw_a, envs=EnvVars({}))
b = VenvSpec(python_version='3.11', requirements_txt=raw_b, envs=EnvVars({}))

print('a 归一化后:\n' + a.normalized_requirements_txt)
print('hash 相同?:', hash(a) == hash(b))
```

**需要观察的现象**：打印出的 `a` 归一化结果中，注释和空行消失、依赖按字母序排列（`bentoml`、`torch`、`vllm`）。

**预期结果**：`hash 相同?: True`。这正是 venv 目录复用的前提——两次 `serve` 同一个 Bento，算出同一个目录名。

> 待本地验证：`python_version` 不同的两个 `VenvSpec`（依赖相同）是否也会哈希相同？根据上面的源码分析，答案应为「相同」。你可以把 `b` 的 `python_version` 改成 `'3.10'` 再跑一次确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `VenvSpec.__hash__` 用 `md5(...)` 而不是直接 `hash(self.requirements_txt)`？

**参考答案**：内置 `hash(str)` 在 Python 启动时受 `PYTHONHASHSEED` 随机化影响，不同进程结果不同。而 venv 目录名要跨进程（跨两次 `openllm serve` 调用）稳定，`md5` 是确定性的，保证「同一份依赖 → 同一个目录名」。

**练习 2**：如果 `bento.yaml` 里只改了一条注释，会让 venv 目录被重新创建吗？

**参考答案**：不会。注释行（`#` 开头）在 `normalized_requirements_txt` 里被丢弃，不进入哈希，所以目录名不变、`_ensure_venv` 会直接复用旧环境。

---

### 4.2 用 uv 创建环境并安装依赖

#### 4.2.1 概念说明

知道了「规格 → 哈希 → 目录名」，接下来要解决的是「目录不存在时如何把它装出来」。OpenLLM 选择 [`uv`](https://github.com/astral-sh/uv)（一个用 Rust 写的极快包管理器）来完成两件事：

1. **创建虚拟环境**：`uv venv <path> -p <python_version>`，按指定 Python 版本建一个空环境；
2. **安装依赖**：`uv pip install -p <venv里的python> <包或-r requirements.txt>`，把包装进这个环境。

而要拿到「规格」，需要先从 Bento 目录里读出两份东西：依赖清单（优先锁文件 `requirements.lock.txt`，回退 `requirements.txt`）和元信息（`bento.yaml` 里的 `python_version`、`envs`）。这部分由 `_resolve_bento_venv_spec` 完成。

#### 4.2.2 核心流程

完整链路（从 `local.py` 触发）：

```
local.serve / _run_model
   │  env = {BENTOML_HOME, ...用户 --env 透传的变量}
   ▼
ensure_venv(bento, runtime_envs=env)
   │
   ▼
_resolve_bento_venv_spec(bento, runtime_envs)      # 带 @lru_cache
   │  1. 读 env/python/requirements.lock.txt，不存在则读 requirements.txt
   │  2. 读 bento.yaml → image.python_version、envs
   │  3. envs：bento.yaml 声明的变量，若 runtime_envs 提供则覆盖其值
   │  4. 组装 VenvSpec(python_version, requirements_txt, name_prefix, envs)
   ▼
_ensure_venv(venv_spec)
   │  venv = VENV_DIR / str(hash(venv_spec))
   │  （见 4.3：命中缓存就直接返回）
   │  未命中时：
   │    uv venv <venv> -p python_version            # 建空环境
   │    uv pip install -p <venv_py> bentoml          # 先装 bentoml
   │    写 venv/requirements.txt（归一化后的依赖）
   │    uv pip install -p <venv_py> -r requirements.txt  # 装其余依赖
   │    写 venv/DONE 标记
   ▼
返回 venv 目录路径 → local 拿它去 run_command(..., venv=venv)
```

注意 `bentoml` 是**单独先装**的，而不是写进 `requirements.txt` 一起装。这是因为 `bentoml serve` 是最终要执行的命令，OpenLLM 希望确保它一定在环境里（即使 Bento 自己的依赖清单没列）。

#### 4.2.3 源码精读

先看规格解析。`_resolve_bento_venv_spec` 带 `@functools.lru_cache`，意味着**同一进程内对同一个 `(bento, runtime_envs)` 只解析一次**：

[venv.py:L17-L36](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L17-L36) —— 要点：

- **锁文件优先**：先找 `env/python/requirements.lock.txt`，不存在才回退 `requirements.txt`（[venv.py:L19-L21](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L19-L21)）。锁文件锁死版本，保证可复现。
- **从 `bento.yaml` 读 Python 版本**：`data['image']['python_version']`（[venv.py:L24-L27](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L24-L27)）。
- **环境变量的合并语义**：`bento_envs` 来自 `bento.yaml` 的 `envs` 列表（`{name: value}`）；如果调用方传了 `runtime_envs`，则「Bento 声明的每个变量，若 runtime 提供就用 runtime 的值，否则保留 Bento 默认值」（[venv.py:L28-L29](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L28-L29)）。这正是 `serve --env HF_TOKEN=xxx` 能覆盖默认值的来源。
- **`name_prefix`** 用 `bento.tag`（把 `:` 换成 `_`）拼成，仅作 `uv venv` 命名用。

再看 `_ensure_venv` 的「创建并安装」部分（缓存命中分支留到 4.3 讲）：

[venv.py:L39-L58](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L39-L58) —— 步骤：

1. `venv = VENV_DIR / str(hash(venv_spec))`：用哈希定目录（[venv.py:L40](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L40)）。
2. `venv_py`：跨平台取环境内解释器，Windows 是 `Scripts\python.exe`，类 Unix 是 `bin/python`（[venv.py:L46](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L46)）。
3. **`uv venv` 建环境**：`['python', '-m', 'uv', 'venv', venv, '-p', python_version]`（[venv.py:L48-L51](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L48-L51)）。这里的 `'python'` 会被 `run_command` 重写成当前解释器——也就是 OpenLLM 自己所在的 Python。所以前提是 **OpenLLM 的环境里装了 `uv`**（`pyproject.toml` 确实把 `uv` 列为依赖，见 [pyproject.toml:L43](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/pyproject.toml#L43)）。
4. **先装 `bentoml`**：`uv pip install -p <venv_py> bentoml`（[venv.py:L52-L56](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L52-L56)）。
5. **写归一化后的 requirements**：把 `venv_spec.normalized_requirements_txt` 写进 `venv/requirements.txt`（[venv.py:L57-L58](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L57-L58)）——注意写的是**归一化后**的版本，注释/空行已被清掉。

接着用这个文件装其余依赖并写 `DONE`：

[venv.py:L59-L75](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L59-L75) —— `uv pip install -p <venv_py> -r requirements.txt` 装全部依赖（带 `env=venv_spec.envs`，让如 `HF_TOKEN` 这类变量在安装期可见），成功后写一个内容为 `'DONE'` 的标记文件。

每条 `run_command` 都带了 `silent=VERBOSE_LEVEL.get() < 10`：默认 `VERBOSE_LEVEL=0` 时 `silent=True`（uv 的输出被静默），加了 `--verbose`（设为 20）时 `silent=False`，能在终端看到完整的 `uv` 安装日志——这对排查「为什么装不上」非常关键。

公开入口 `ensure_venv` 只是把两步串起来：

[venv.py:L88-L92](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L88-L92) —— 注意它把调用方传入的 `runtime_envs` 包成了 `EnvVars`（去空、排序），保证进入 `_resolve_bento_venv_spec`（从而进入哈希）的是规整后的环境变量。

#### 4.2.4 代码实践

**实践目标**：用源码阅读确认「bentoml 为何被单独先装」「依赖文件为何要重写一份」。

**操作步骤**（源码阅读型实践，无需运行模型）：

1. 打开 [venv.py:L48-L75](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L48-L75)。
2. 回答两个问题：
   - 为什么有 [L52-L56](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L52-L56) 的 `uv pip install ... bentoml`，又要在 [L59-L73](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L59-L73) 再 `uv pip install -r requirements.txt`？能不能合成一次？
   - [L57-L58](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L57-L58) 写入的是 `venv_spec.normalized_requirements_txt` 而不是原始 `requirements_txt`，这样做的好处是什么？

**需要观察的现象/预期结果**：

- `bentoml` 被单独先装，是为了让「最终要执行的 `bentoml serve`」必定可用，不依赖 Bento 的依赖清单是否列出它；两步分开也隔离了「装 bentoml 失败」和「装模型依赖失败」两种错误。理论上可以合并成一次 `uv pip install bentoml -r requirements.txt`，但当前实现选择了显式分步。
- 写归一化版本的好处是：去掉注释/空行、排序后，`uv` 看到的是一份干净确定的清单，也方便你直接打开 `venv/requirements.txt` 排查实际装了什么。

> 待本地验证：在有网络的机器上 `openllm serve <一个小模型> --verbose`，对照终端里 uv 的真实输出，确认执行顺序确实是「venv → install bentoml → install -r requirements.txt」。

#### 4.2.5 小练习与答案

**练习 1**：`_resolve_bento_venv_spec` 上的 `@functools.lru_cache` 缓存的是什么？它和磁盘上的 venv 缓存是一回事吗？

**参考答案**：它缓存的是「`BentoInfo` + `runtime_envs` → `VenvSpec` 对象」的**进程内**映射，避免在一次 `openllm` 运行里反复读文件解析。它和磁盘上的 venv 缓存（`VENV_DIR/<hash>`）是两层不同的缓存：前者省的是「解析」，后者省的是「安装」。

**练习 2**：`uv pip install` 时传了 `env=venv_spec.envs`，如果 Bento 声明了 `HF_TOKEN` 但用户没提供，会发生什么？

**参考答案**：`_resolve_bento_venv_spec` 中 `envs = {k: runtime_envs.get(k, v) ...}`，没提供则用 Bento 默认值（可能是空）。随后 `EnvVars` 构造会去掉空值，于是 `HF_TOKEN` 不会出现在安装环境里。安装期若需要下载 gated 模型权重可能会失败——但通常权重下载发生在 `bentoml serve` 运行期而非安装期，运行期的环境变量由 `local.py` 的 `env` 另行注入。

---

### 4.3 缓存复用与失败清理

#### 4.3.1 概念说明

建一个 venv 可能动辄下载几 GB 的 torch / vLLM，非常慢。所以「**能复用就绝不重装**」是这一节的核心诉求。OpenLLM 用两个机制保证这一点：

- **以哈希为目录名**：同一份依赖 → 同一哈希 → 同一目录。第二次 `_ensure_venv` 发现目录已存在，直接返回，跳过全部安装。
- **`DONE` 标记**：安装是分多步的（建环境 → 装 bentoml → 装依赖），中间任何一步崩了，目录就是个「半成品」。OpenLLM 用一个 `DONE` 文件标记「全部装完」，并规定：**目录存在但没有 `DONE` 视为脏，要先删再装。**

同时，安装失败必须**清理干净**，否则下次还会命中一个坏掉的目录。这两条规则共同决定了 `_ensure_venv` 的三分支结构。

#### 4.3.2 核心流程

`_ensure_venv` 的判定逻辑（伪代码）：

```
venv = VENV_DIR / str(hash(venv_spec))

if 目录存在 且 没有 DONE:      # 上次装到一半挂了 / 被外部中断
    rmtree(venv)              # 当作脏数据删掉

if 目录不存在:                 # 全新或刚被清理
    try:
        uv venv ...           # 建环境
        uv pip install bentoml
        写 requirements.txt
        uv pip install -r ...
        写 DONE               # 全部成功的「句号」
    except 任何异常:
        rmtree(venv)          # 失败必清，避免留半成品
        若 verbose 打印错误
        打印 "Failed ... Cleaned up."
        raise typer.Exit(1)   # 整个命令退出
    打印 "Successfully installed"
    return venv
else:                          # 目录存在 且 有 DONE → 缓存命中
    return venv                # 直接复用，零安装
```

这里有一个简洁的对称美：**成功路径在最后写 `DONE`，失败路径在 except 里删目录**。两条路径都不会留下「半成品」——要么是带 `DONE` 的完整环境，要么根本不存在。

#### 4.3.3 源码精读

脏数据清理在函数最开头：

[venv.py:L41-L42](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L41-L42) —— `if venv.exists() and not (venv / 'DONE').exists()`：目录在但没有 `DONE`，说明上次没装完，`shutil.rmtree(venv, ignore_errors=True)` 删掉重来。

成功路径写 `DONE`：

[venv.py:L74-L75](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L74-L75) —— 整个 `try` 块跑完无异常，才写这个标记。它的存在是「这个环境可用」的唯一凭证。

失败清理与退出：

[venv.py:L76-L81](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L76-L81) —— 任何异常都 `rmtree(venv)`，当 `VERBOSE_LEVEL >= 10`（即 `--verbose`）时用红色打印异常详情，再打印一句「Failed to install dependencies to ... Cleaned up.」，最后 `raise typer.Exit(1)` 让整个 CLI 以非零码退出。注意这里把异常「吞掉」换成统一的退出码——上游 `local.py` 不会看到一个原始的 `uv` 报错栈，而是一个干净的失败终止。

缓存命中的快路径：

[venv.py:L82-L85](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L82-L85) —— 注意「成功打印」只在**新装**时出现；命中缓存走 `else` 分支，**静默返回**，连「Successfully installed」都不会打印。所以你第二次 `serve` 同一个模型时看不到安装日志，这正是缓存生效的可观测信号。

最后是只读探测函数 `check_venv`：

[venv.py:L95-L102](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L95-L102) —— 它和 `_ensure_venv` 用**同一套哈希定位**（`VENV_DIR / str(hash(venv_spec))`），但只判断「目录存在 且 有 `DONE`」，绝不触发安装。适用于「只想知道这个模型是不是已经准备好」的场景（例如 `hello` 里给已就绪模型打勾），避免误装。

#### 4.3.4 代码实践

**实践目标**：亲手触发一次安装失败，验证 venv 被正确清理，并用 `--verbose` 看到错误。

**操作步骤**（需本机装有 `uv` 且能联网；`uv` 已是 OpenLLM 依赖，开发安装后即有）：

1. 把下面这段保存为 `play_venv_fail.py`（示例代码）。它**不走真实 Bento**，而是直接构造一个含「不存在包」的 `VenvSpec`，逼 `uv pip install` 失败：

```python
# 文件名: play_venv_fail.py
from openllm.common import VERBOSE_LEVEL, VenvSpec, EnvVars
from openllm.venv import _ensure_venv, VENV_DIR  # VENV_DIR 来自 common，这里经 venv 模块转出

# 打开详细日志：让 except 里 VERBOSE_LEVEL.get() >= 10 成立，能看到红色错误
VERBOSE_LEVEL.set(20)

spec = VenvSpec(
    python_version='3.11',
    requirements_txt='this-package-does-not-exist-xyz==99.99.99',
    envs=EnvVars({}),
    name_prefix='fake-1-',
)

venv_path = VENV_DIR / str(hash(spec))
print('安装前目录是否存在:', venv_path.exists())

try:
    _ensure_venv(spec)
except SystemExit as e:
    print('捕获到退出码:', e.code)

print('安装后目录是否存在(应为 False，即已清理):', venv_path.exists())
```

2. 运行：`python play_venv_fail.py`。

**需要观察的现象**：

- 终端出现红色 `Failed to install dependencies to ... Cleaned up.`；
- （因为 `VERBOSE_LEVEL.set(20)`）还能看到红色的异常详情，说明命中了 [venv.py:L78-L79](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L78-L79) 的 `if VERBOSE_LEVEL.get() >= 10` 分支；
- 脚本「安装前/后」两次检查，目录都不存在（失败后被 `rmtree`）。

**预期结果**：`安装后目录是否存在(应为 False): False`，且进程以非零码退出（`_ensure_venv` 内 `raise typer.Exit(1)` 在脚本里表现为 `SystemExit(code=1)`）。

> 待本地验证：不同机器上 `uv` 的报错文案略有差异，但「目录被清理 + 非零退出」两条不变。若想对比「成功缓存」，可把 `requirements_txt` 换成一个真实存在的小包（如 `pydantic`）连跑两次：第一次打印 `Installing model dependencies(...)` 与 `Successfully installed`，第二次两者都消失（命中 `else` 快路径）。

#### 4.3.5 小练习与答案

**练习 1**：如果不写 `DONE` 标记，仅靠「目录是否存在」判断缓存，会出什么问题？

**参考答案**：安装是多步的。如果建好环境、装好 bentoml 后，在装模型依赖时进程被 `Ctrl-C` 中断，目录已经存在但依赖不全。下次 `_ensure_venv` 会误以为「已装好」直接返回，`bentoml serve` 启动时却因缺包崩溃，且错误极难定位。`DONE` 把「目录在」和「完整装好」区分开，脏目录会被 [venv.py:L41-L42](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L41-L42) 清掉重装。

**练习 2**：为什么 `_ensure_venv` 的 `except` 要 `raise typer.Exit(1)` 而不是把异常原样抛出？

**参考答案**：为了让 CLI 给出一个干净、统一的失败终止。`uv` 失败的原始异常栈对普通用户没有意义，统一转成 `typer.Exit(1)` 既保证了非零退出码（便于脚本/CI 判断成败），又避免了上游 `local.py` 还要额外处理一类异常。代价是丢失了原始栈——所以才用 `VERBOSE_LEVEL >= 10` 时打印异常详情作为补偿。

## 5. 综合实践

把本讲三块内容串起来，完成一次「带缓存的依赖准备」全流程观察。请在一台能联网、已 `pip install -e .` 开发安装 OpenLLM 的机器上进行：

1. **准备**：选一个体积小的真实模型（例如 `llama3.2:1b`），先确保仓库已更新（`openllm repo update`）。
2. **首次安装**：运行 `openllm serve llama3.2:1b --verbose`（`--verbose` 让 `VERBOSE_LEVEL=20`，从而 `silent=False`，能看到 uv 全过程）。在安装阶段按 `Ctrl-C` 中断它。
3. **验证脏清理**：到 `~/.openllm/venv/` 下找对应哈希目录，确认它**存在但没有 `DONE`** 文件（半成品）。再次运行同一条命令，观察日志：因为 [venv.py:L41-L42](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L41-L42) 会先删掉这个脏目录，所以会重新看到 `Installing model dependencies(...)`。
4. **验证缓存命中**：让这次安装跑完（看到 `Successfully installed` 和 `DONE` 文件）。第三次运行同一命令，确认**不再出现**安装日志、直接进入 `bentoml serve`（命中 [venv.py:L84-L85](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/venv.py#L84-L85) 的快路径）。
5. **手算哈希**：用 4.1.4 的方法，在 Python 里 `_resolve_bento_venv_spec(ensure_bento(...))` 拿到 `VenvSpec`，打印 `hash(spec)`，再与 `~/.openllm/venv/` 下的实际目录名比对，确认二者一致——这就是「规格 → 哈希 → 目录」链路的闭环。

> 若本机无 GPU / 无法真正 serve，做到第 4 步「依赖装完、`DONE` 写出、再次静默复用」即可证明缓存机制生效；`bentoml serve` 是否能真正起服务属于下一讲 [u3-l1](u3-l1-local-serve-run.md) 的范畴。

## 6. 本讲小结

- OpenLLM 不自带推理依赖，而是用 `uv` 为每个 Bento 在 `~/.openllm/venv/<hash>` 下单独建虚拟环境，再让 `bentoml serve` 在其中运行。
- `VenvSpec` 是「环境规格」：`normalized_requirements_txt` 把依赖排序、去注释/空行；`__hash__` 用确定性的 `md5` 生成跨进程稳定的目录名，实现「同一份依赖只装一次」。常规路径下真正决定哈希的是依赖文本。
- `_resolve_bento_venv_spec`（带 `lru_cache`）从 Bento 目录读 `requirements.lock.txt`/`requirements.txt` 与 `bento.yaml`，并把 `--env` 透传的变量合并进 `envs`。
- `_ensure_venv` 三分支：脏目录（有目录无 `DONE`）先删、全新则 `uv venv` → 装 `bentoml` → 写归一化 `requirements.txt` → `uv pip install -r` → 写 `DONE`、缓存命中（有目录有 `DONE`）直接返回。
- 失败必清理：`except` 里 `rmtree` + （verbose 时）打印错误 + `typer.Exit(1)`，绝不留半成品；`--verbose`（`VERBOSE_LEVEL=20`）是查看 uv 安装/报错日志的开关。
- `check_venv` 复用同一套哈希定位做轻量「是否就绪」探测，不触发安装。

## 7. 下一步学习建议

本讲讲清了「环境怎么准备」，但还差最后一步把整条 `serve` 链路闭环——拿到 venv 之后，`local.py` 如何拼装 `bentoml serve` 命令、如何用 `run_command(..., venv=venv)` 让服务在这个环境里跑起来，以及 `run` 模式如何轮询 `/readyz`。这些正是下一讲 [u3-l1 本地 serve 与 run 的完整链路 local.py](u3-l1-local-serve-run.md) 的内容，建议紧接着阅读。

此外，如果你想了解「这些 venv 缓存占满了磁盘怎么办」，可以跳读 [u3-l4 磁盘清理与缓存管理 clean.py](u3-l4-disk-cleanup.md)，其中的 `clean venvs` 子命令正是清理本讲产生的 `VENV_DIR`。
