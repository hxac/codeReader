# 仓库结构与多代代码共存

## 1. 本讲目标

本讲承接 [u1-l1（FlashAttention 是什么）]：你已经知道 FlashAttention 用「分块 + 在线 softmax」把注意力的额外显存从 \(O(N^2)\) 降到 \(O(N)\)，也知道了 FA1→FA4 的演进脉络。但当你真正 `git clone` 这个仓库时，会发现一件让人困惑的事——**同一个仓库里同时躺着三代实现**，而且互相之间还能共存。

学完本讲，你应该能够：

1. 看懂仓库根目录每个关键文件夹 / 文件的职责。
2. 准确区分 FA2、FA3、FA4 三代实现**各自所在的目录**、所用的语言、打包后的包名、以及它们面向的 GPU 架构。
3. 说清楚为什么 `from flash_attn import flash_attn_func`（FA2）和 `from flash_attn.cute import flash_attn_func`（FA4）能并存——也就是 `setup.py`、`pkgutil.extend_path` 和 FA4 子包 `pyproject.toml` 之间是怎么配合的。

本讲**不深入 kernel 源码**，只解决「我在哪、东西在哪」的导航问题。后续每一讲都会落到某个具体目录，所以先把地图背熟。

## 2. 前置知识

- **包（package）与模块（module）**：Python 里一个目录只要带 `__init__.py` 就是一个包；`import flash_attn.cute` 表示先找 `flash_attn` 包，再在它里面找 `cute` 子包。
- **命名空间包（namespace package）**：普通包的 `__path__` 只指向自己所在目录；命名空间包允许**多个不同目录**共同贡献到同一个包名下。本仓库用 `pkgutil.extend_path` 实现这一点，这是 FA2 与 FA4 能共存的底层机制。
- **构建脚本**：`setup.py`（传统 setuptools 脚本）和 `pyproject.toml`（现代 PEP 517/518 构建配置）都是「怎么把源码打包成可安装的 wheel」的说明书。本仓库三代实现各用一套。
- **JIT 编译（即时编译）**：FA4 用 CuTeDSL（Python）写 kernel，运行时才编译成 PTX/CUBIN；而 FA2/FA3 是在 `pip install` 时就用 nvcc/hipcc 把 C++/CUDA 编译成机器码。这点差异决定了它们安装方式的不同。
- **GPU 架构代号**：sm_80（Ampere，如 A100）、sm_90（Hopper，如 H100）、sm_100/110/120（Blackwell 家族，如 B200）。FA2 主打 Ampere，FA3 专攻 Hopper，FA4 覆盖 Hopper + Blackwell。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| `README.md` | 项目总说明，分别在三个章节介绍 FA2 / FA3 / FA4 的安装与用法 |
| `setup.py` | **FA2 的构建入口**：编译 `csrc/flash_attn/` 下的 CUDA 源码，产出 `flash_attn` 包 |
| `flash_attn/__init__.py` | FA2 包入口，用 `extend_path` 让 FA2 与 FA4 子包共存 |
| `flash_attn/flash_attn_interface.py` | FA2 的 Python 接口（`flash_attn_func` 等真正定义在这里） |
| `flash_attn/cute/` | **FA4 子包**：纯 Python + CuTeDSL，本手册主线 |
| `flash_attn/cute/__init__.py` | FA4 包入口，导出 `flash_attn_func` / `flash_attn_varlen_func` |
| `flash_attn/cute/pyproject.toml` | **FA4 的构建配置**：包名 `flash-attn-4`，纯 Python，运行时 JIT |
| `flash_attn/cute/README.md` | FA4 的安装与用法速览 |
| `hopper/` | **FA3** 目录：C++/CUDA，自带 `hopper/setup.py`，产出 `flash_attn_3` 包 |
| `csrc/` | FA2 的 C++/CUDA 源码（以及 ROCm 后端、辅助算子） |
| `tests/` | 测试：`tests/cute/` 给 FA4，其余给 FA2 及其生态 |
| `benchmarks/`、`AI/`、`training/`、`examples/` | 基准、调试笔记、训练脚本、推理示例 |

> 小提示：本讲引用的所有永久链接都指向当前 HEAD `1f7ce2f7`。

## 4. 核心概念与源码讲解

### 4.1 根目录与三代实现定位

#### 4.1.1 概念说明

很多开源项目「新版本覆盖旧版本」，但 FlashAttention 不是。它把 **FA2、FA3、FA4 三代实现同时放在一个仓库**里，原因有三：

1. **硬件代差**：FA2 主要跑 Ampere（A100）级别，FA3 专门为 Hopper（H100）优化，FA4 面向 Hopper + Blackwell（B200）。用户手里的 GPU 决定了该用哪一代，旧硬件用不了新指令。
2. **实现语言不同**：FA2/FA3 是手写 C++/CUDA（编译期就定死），FA4 是 CuTeDSL（Python 描述、运行时编译）。两套技术栈并存，便于团队同时维护和迭代。
3. **渐进迁移**：FA4 仍是 Alpha 阶段（见 `pyproject.toml` 里的 `Development Status :: 3 - Alpha`），所以 FA2/FA3 作为稳定后备长期保留。

三代实现的定位对照：

| 代 | 代码目录 | 语言 | 安装后包名 | 导入方式 | 目标 GPU |
| --- | --- | --- | --- | --- | --- |
| FA2 | `flash_attn/`（接口）+ `csrc/flash_attn/`（CUDA 内核） | C++/CUDA | `flash-attn`（v2.8.4） | `from flash_attn import flash_attn_func` | Ampere/Ada/Hopper |
| FA3 | `hopper/` | C++/CUDA | `flash-attn-3` | `from flash_attn_3 import flash_attn_interface` | Hopper（H100） |
| FA4 | `flash_attn/cute/` | Python + CuTeDSL | `flash-attn-4` | `from flash_attn.cute import flash_attn_func` | Hopper + Blackwell |

> 顺带一提：FA2 在 AMD GPU 上还有两条 ROCm 路径——composable_kernel（ck，默认）后端源码在 `csrc/flash_attn_ck/`，Triton 后端由 `third_party/aiter` 子模块提供。本手册不展开 ROCm，但你要知道它存在。

#### 4.1.2 核心流程

读者面对这个仓库时，「挑哪一代」的决策流程可以用下面这段伪代码概括：

```
你的 GPU 是什么？
├─ Ampere / Ada (A100, RTX 30/40)  → 用 FA2:  pip install flash-attn        → from flash_attn import ...
├─ Hopper (H100/H800)，想要极致 FP16/BF16/FP8 → 用 FA3: cd hopper && python setup.py install
│                                                                  → from flash_attn_3 import ...
└─ Hopper 或 Blackwell，想要最新 CuTeDSL 路线 → 用 FA4:  pip install flash-attn-4 → from flash_attn.cute import ...
```

注意三个导入路径在字面上很像（都有 `flash_attn` 字样），但它们指向**三个不同的包**：

- `flash_attn` → FA2（顶层包）
- `flash_attn_3` → FA3（独立顶层包，不带下划线变体）
- `flash_attn.cute` → FA4（`flash_attn` 这个命名空间下的 `cute` 子包）

这正是本讲后面要解释的「共存机制」的妙处：FA4 借用了 FA2 的 `flash_attn` 名字空间，但内容完全独立。

#### 4.1.3 源码精读

先看 `README.md` 是怎么分别交代三代用法的。FA4 这一节明确给出了导入语句：

[README.md:L80-L99](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md#L80-L99) —— 这段说明 FA4 用 CuTeDSL 编写、面向 Hopper 与 Blackwell，安装命令是 `pip install flash-attn-4`，用法是 `from flash_attn.cute import flash_attn_func`。

FA3 这一节则在安装和导入上和 FA4 完全不同——它要求你 `cd hopper` 后再 `python setup.py install`，并且导入的是 `flash_attn_3`：

[README.md:L49-L63](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/README.md#L49-L63) —— FA3 的安装与导入说明，注意它的包名是 `flash_attn_3`，与 FA4 的 `flash_attn.cute` 区分开。

再看 FA4 自己的包入口，确认它确实只导出两个函数：

[flash_attn/cute/__init__.py:L10-L18](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/__init__.py#L10-L18) —— FA4 子包从 `.interface` 模块导出 `flash_attn_func` 和 `flash_attn_varlen_func`，这是 FA4 唯一的公开 API（本手册后续会深入 `interface.py`）。

对比一下 FA3 的接口所在文件（仅作定位，不展开）：

[hopper/flash_attn_interface.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/hopper/flash_attn_interface.py) —— FA3 的 Python 接口文件，调用编译好的 C++/CUDA 扩展。

> 关键认知：**三代实现的 `flash_attn_func` 同名但不同源**。FA2 的来自 `flash_attn.flash_attn_interface`（C++ 扩展），FA4 的来自 `flash_attn.cute.interface`（CuTeDSL）。下一讲的 [u1-l4] 会专门用脚本验证这一点。

#### 4.1.4 代码实践

**实践目标**：用一段脚本「自报家门」，确认三个 `flash_attn_func` 各自来自哪个文件。

**操作步骤**：

1. 把下面的脚本存为 `who_is_fa.py`（示例代码，非项目原有文件）：

```python
# 示例代码：打印三代 flash_attn_func 的来源
def show(name, fn):
    print(f"{name}: module={getattr(fn, '__module__', '?')}, file={getattr(fn, '__module__', '?')}")

# FA4（最可能单独安装成功）
try:
    from flash_attn.cute import flash_attn_func as fa4
    show("FA4 (flash_attn.cute)", fa4)
except Exception as e:
    print("FA4 不可用:", e)

# FA2（需要已编译 C++ 扩展 flash_attn_2_cuda）
try:
    from flash_attn import flash_attn_func as fa2
    show("FA2 (flash_attn)", fa2)
except Exception as e:
    print("FA2 不可用:", e)

# FA3（需要 hopper 已安装）
try:
    from flash_attn_3.flash_attn_interface import flash_attn_func as fa3
    show("FA3 (flash_attn_3)", fa3)
except Exception as e:
    print("FA3 不可用:", e)
```

2. 运行 `python who_is_fa.py`。

**需要观察的现象**：每个能成功导入的函数，其 `__module__` 应分别形如 `flash_attn.cute.interface`、`flash_attn.flash_attn_interface`、`flash_attn_3.flash_attn_interface`。

**预期结果**：如果你只装了 FA4，那么只有第一行打印成功，后两行报 `ModuleNotFoundError`——这本身就证明了三者是**三个独立包**。

**说明**：FA2 需要 nvcc 编译、FA3 需要 Hopper 环境编译，本机若不具备条件则对应分支不可用，属正常现象。具体打印值「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：小明说「`from flash_attn import flash_attn_func` 和 `from flash_attn.cute import flash_attn_func` 是同一个函数」。对吗？为什么？

> **答案**：不对。前者来自 FA2（`flash_attn.flash_attn_interface`，背后是 C++/CUDA 扩展 `flash_attn_2_cuda`），后者来自 FA4（`flash_attn.cute.interface`，背后是 CuTeDSL 运行时编译的 kernel）。它们只是碰巧同名。

**练习 2**：如果你的显卡是 RTX 4090（Ada，sm_89），三代里你**不能**正常使用哪一代？为什么？

> **答案**：FA3。FA3 专门为 Hopper（H100/H800）优化，依赖 Hopper 专有指令；RTX 4090 属于 Ada/Ampere 系，应使用 FA2（或可尝试 FA4 的 Ampere/兼容路径）。

**练习 3**：FA4 的代码放在 `flash_attn/cute/`，而 FA2 的代码放在顶层 `flash_attn/`。这种「FA4 嵌在 FA2 名下」的结构会让人混淆吗？它带来的最大好处是什么？

> **答案**：会有一定混淆，但好处是**共用 `flash_attn` 命名空间**——用户 `pip install` 两个包后，可以同时 `import flash_attn`（FA2）和 `from flash_attn.cute import ...`（FA4）而互不冲突，便于在新旧实现之间平滑切换与对比。

---

### 4.2 测试、示例、基准与调试目录

#### 4.2.1 概念说明

除了三代 kernel 源码，仓库里还有一批「围绕主代码」的辅助目录，理解它们能帮你快速验证、压测和排错：

- **`tests/`**：测试套件。FA4 的测试集中在 `tests/cute/`（如 `test_flash_attn.py`、`test_flash_attn_varlen.py`、`test_mask_mod.py`、`test_score_mod.py`、`test_block_sparsity.py`）；FA2 的测试在 `tests/test_flash_attn.py`、`tests/test_flash_attn_ck.py` 等；`tests/models/`、`tests/layers/`、`tests/ops/` 则是 FA2 生态（rotary、layer_norm、fused_dense、GPT/ViT 等模型）的测试。FA3 把自己的测试放在 `hopper/test_flash_attn.py`。
- **`benchmarks/`**：基准测试脚本，测量前向/反向在不同 seqlen 下的吞吐（TFLOPs）。FA4 另有更细的基准与配置搜索在 `flash_attn/cute/` 内（`benchmark.py`、`bench_utils.py`、`sm90_config_search.py`）。
- **`training/`**：一套完整的 GPT 训练脚本与 Hydra 配置，演示如何把 FlashAttention 端到端拼进模型训练。
- **`examples/inference/`**：推理示例（目前主要是说明文档）。
- **`AI/`**：这是最特别的目录——它不是源码，而是**调试笔记与复现脚本**（lab notes），记录了 2CTA 死锁排查、TMA 竞争假阳性、CLC 调度 trace 可视化等一线经验。FA4 kernel 出问题时，这里是第一手资料。

#### 4.2.2 核心流程

把「我想做某件事」映射到「该去哪个目录」：

```
想验证 FA4 数值正确性        → tests/cute/test_flash_attn.py（含参考实现 attention_ref）
想测 FA4 在某 seqlen 的吞吐  → flash_attn/cute/benchmark.py 或顶层 benchmarks/
想跑端到端 GPT 训练验证      → training/（Hydra 配置 + 脚本）
FA4 kernel 卡死 / 悬挂       → AI/DEBUG_2CTA.md、AI/RACECHECK_TMA_HAZARD.md
想看 CLC 调度到底排了什么    → AI/CLC_TRACE_DEBUG.md + AI/parse_clc_log.py
```

#### 4.2.3 源码精读

先看 FA4 主测试文件是怎么组织的——它直接从 `flash_attn.cute.testing` 引入参考实现 `attention_ref` 和数据生成工具：

[tests/cute/test_flash_attn.py:L21-L30](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/tests/cute/test_flash_attn.py#L21-L30) —— FA4 测试从 `flash_attn.cute.testing` 导入 `attention_ref`（参考注意力实现）和 `generate_qkv`（数据生成）。这告诉我们：FA4 自带一套「黄金参考」，所有 kernel 都在和它对拍。

再看调试笔记目录的代表条目：

[AI/DEBUG_2CTA.md:L1-L14](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/AI/DEBUG_2CTA.md#L1-L14) —— 这篇笔记讲如何排查 GPU kernel 悬挂/死锁，核心方法是「最小复现 + 用带线程守卫的 `cute.printf` 做二分定位」。它是后面 [u11-l5（GPU 调试）] 的预习材料。

最后看训练目录的自我介绍：

[training/README.md:L1-L9](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/training/README.md#L1-L9) —— 说明 `training/` 是「把 FlashAttention 集成进 GPT/ViT 并端到端训练」的示例，附带 MLP/LayerNorm/交叉熵等优化算子，目标是演示如何把组件拼成完整模型。

> 文件级入口（不展开行号）：FA4 基准 [flash_attn/cute/benchmark.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/benchmark.py)、FA2 基准 [benchmarks/benchmark_flash_attention.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/benchmarks/benchmark_flash_attention.py)、CLC 调度日志解析 [AI/parse_clc_log.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/AI/parse_clc_log.py)。

#### 4.2.4 代码实践

**实践目标**：用一条只读 git 命令，数清楚 `tests/` 下 FA4 测试与 FA2 测试各自的文件数量，从而在脑子里建立「测试目录也分代」的直觉。

**操作步骤**：

```bash
# 列出 tests/cute/ 下的测试文件（FA4）
git -C <仓库根目录> ls-files 'tests/cute/test_*.py'
# 列出 tests/ 顶层的 FA2 测试
git -C <仓库根目录> ls-files 'tests/test_*.py'
```

**需要观察的现象**：`tests/cute/` 下应出现 `test_flash_attn.py`、`test_flash_attn_varlen.py`、`test_mask_mod.py`、`test_score_mod.py`、`test_block_sparsity.py`、`test_flash_attn_combine.py` 等；而 `tests/` 顶层会出现 `test_flash_attn.py`、`test_flash_attn_ck.py`、`test_flash_attn_triton_amd.py` 等。

**预期结果**：两组文件互不重叠——FA4 的测试全部带 `cute/` 前缀，FA2 的测试在顶层。这印证了「目录分代」的组织原则。

**说明**：本实践为源码阅读型，无需 GPU；具体文件列表「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：你想给 FA4 提一个 `mask_mod`（自定义掩码）的 bug 复现，应该往哪个测试文件加用例？参考实现在哪？

> **答案**：测试加在 `tests/cute/test_mask_mod.py`（掩码定义可参考 `tests/cute/mask_mod_definitions.py`）；参考实现是 `flash_attn.cute.testing` 里的 `attention_ref`。

**练习 2**：`AI/` 目录里的 `.md` 文件是给谁看的？它和 `flash_attn/cute/` 源码是什么关系？

> **答案**：`AI/` 是给**kernel 开发/调试者**看的实验笔记，记录死锁、竞争、调度可视化等排错经验。它不是产品代码，而是解释源码为什么会出问题、怎么查——可以理解为「源码的旁注」。

**练习 3**：为什么 FA4 把基准脚本 `benchmark.py` 放在 `flash_attn/cute/` 包**内部**，而 FA2 的基准放在仓库顶层 `benchmarks/`？

> **答案**：FA4 的基准和配置搜索（`sm90_config_search.py`）与 kernel 特化强耦合，常被当作「随包分发的开发工具」一起维护；FA2 的基准更偏「外部压测脚本」，所以放在顶层独立目录。两者只是组织习惯不同。

---

### 4.3 构建脚本与子包配置：三代怎么并存

#### 4.3.1 概念说明

这是本讲最关键、也最容易卡住初学者的一点：**三个包凭什么能装在同一个 Python 环境里而不打架？**

答案是三件套配合：

1. **FA2 的 `setup.py` 在打包时把 `flash_attn.cute` 显式排除**——这样 FA2 的 wheel 里**不包含** FA4 代码，FA2 只认领顶层 `flash_attn`。
2. **FA4 的 `pyproject.toml` 把自己声明为 `flash_attn.cute` 子包**——它的 `package-dir` 把 `flash_attn/cute/` 目录映射成 `flash_attn.cute` 包名，FA4 只认领这个子包。
3. **`flash_attn/__init__.py` 用 `pkgutil.extend_path` 把 `flash_attn` 变成命名空间包**——允许 FA2 和 FA4 两个独立安装的 wheel 共同贡献到同一个 `flash_attn` 名字空间下。

此外还有三个**独立的构建入口**，各自产出独立的 wheel：

| 构建文件 | 产出包 | 编译时机 |
| --- | --- | --- |
| `setup.py`（根） | `flash-attn`（FA2） | 安装时 nvcc 编译 C++/CUDA |
| `hopper/setup.py` | `flash-attn-3`（FA3） | 安装时 nvcc 编译 C++/CUDA |
| `flash_attn/cute/pyproject.toml` | `flash-attn-4`（FA4） | **安装时不编译**，运行时 JIT |

特别注意 FA4 的「安装时不编译」——因为它是纯 Python + CuTeDSL，kernel 在你**第一次调用** `flash_attn_func` 时才被编译成 PTX/CUBIN（这点会在 [u11-l1（JIT 与缓存）] 详讲）。这就是为什么 FA4 的 `pyproject.toml` 里**没有** `CUDAExtension`，只有普通 Python 依赖。

#### 4.3.2 核心流程

三套构建管线并行的示意：

```
┌──────────────────────────────────────────────────────────────────┐
│  根 setup.py                                                      │
│  find_packages(exclude=[..., "flash_attn.cute", ...])  ← 关键排除 │
│  CUDAExtension("flash_attn_2_cuda", sources=["csrc/flash_attn/..."])│
│  → 产出 wheel: flash-attn 2.8.4（只含顶层 flash_attn/）          │
└──────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────┐
│  flash_attn/cute/pyproject.toml                                   │
│  packages = ["flash_attn.cute"]                                   │
│  package-dir = {"flash_attn.cute" = "."}                          │
│  → 产出 wheel: flash-attn-4（只含 flash_attn/cute/ 内容）         │
└──────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────┐
│  hopper/setup.py → 产出 wheel: flash-attn-3（独立包名）           │
└──────────────────────────────────────────────────────────────────┘

两个 wheel 都装上后，运行时：
  flash_attn/__init__.py: __path__ = extend_path(__path__, __name__)
  → Python 把「FA2 的 flash_attn/」和「FA4 贡献的 flash_attn.cute」合并到同一命名空间
  → import flash_attn          ✓（FA2）
  → from flash_attn.cute import ... ✓（FA4）
```

#### 4.3.3 源码精读

先看共存的「总开关」——FA2 包入口里的 `extend_path`：

[flash_attn/__init__.py:L1-L6](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/__init__.py#L1-L6) —— 第 1–4 行用 `pkgutil.extend_path` 把 `flash_attn` 变成命名空间包，注释明确写道「让 fa2 和 fa4 可以共存安装」；第 6 行的 `__version__ = "2.8.4"` 也说明这个文件属于 FA2。

再看 FA2 的 `setup.py` 是怎么「不碰 FA4」的——`find_packages` 的排除列表里赫然有 `flash_attn.cute`：

[setup.py:L760-L773](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/setup.py#L760-L773) —— FA2 打包时显式排除 `flash_attn.cute` 与 `flash_attn.cute.*`，确保 FA2 的 wheel 里不带 FA4 代码，避免和 FA4 的 wheel 内容冲突。

同一个 `setup.py` 里，FA2 还声明了它真正要编译的 CUDA 扩展（注意扩展名是 `flash_attn_2_cuda`，源码来自 `csrc/flash_attn/`）：

[setup.py:L339-L343](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/setup.py#L339-L343) —— 定义名为 `flash_attn_2_cuda` 的 CUDA 扩展，第一个源文件就是 `csrc/flash_attn/flash_api.cpp`，后面跟着一大批 `flash_fwd_*_sm80.cu` / `flash_bwd_*_sm80.cu` 内核。这就是 FA2 的 C++/CUDA 真身。

> 顺带一提，FA2 默认编译的架构列表写死在这里：[setup.py:L72-L74](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/setup.py#L72-L74)，默认 `80;90;100;110;120`，可用环境变量 `FLASH_ATTN_CUDA_ARCHS` 覆盖。

然后看 FA4 自己的构建配置——包名是 `flash-attn-4`，纯 Python：

[flash_attn/cute/pyproject.toml:L5-L11](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/pyproject.toml#L5-L11) —— `[project]` 段声明 `name = "flash-attn-4"`，`requires-python = ">=3.10"`，没有 C 扩展字段，说明它是一个纯 Python 包。

FA4 的依赖（运行时 JIT 所需）在这里：

[flash_attn/cute/pyproject.toml:L24-L32](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/pyproject.toml#L24-L32) —— FA4 依赖 `nvidia-cutlass-dsl==4.6.0.dev0`、`torch`、`einops`、`apache-tvm-ffi`、`quack-kernels>=0.5.3` 等。注意没有 nvcc/hipcc——编译发生在运行时。

最关键的「FA4 认领 `flash_attn.cute` 子包」的声明：

[flash_attn/cute/pyproject.toml:L46-L53](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/pyproject.toml#L46-L53) —— `packages = ["flash_attn.cute"]` 配合 `package-dir = {"flash_attn.cute" = "."}`，把当前目录发布成 `flash_attn.cute` 包；`setuptools_scm` 的 `root = "../.."` 说明版本号从仓库根的 `fa4-v*` tag 推导。

最后，FA4 包入口里的版本号取自 `fa4` 这个分发名，与 FA2 的 `2.8.4` 完全独立：

[flash_attn/cute/__init__.py:L5-L8](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/__init__.py#L5-L8) —— FA4 尝试从已安装的 `fa4` 分发元数据读版本，读不到就回落到 `0.0.0`，与 FA2 的版本号互不影响。

> 三个包的版本号各自独立：FA2 在 `flash_attn/__init__.py` 是 `2.8.4`；FA4 在 `pyproject.toml` 由 setuptools-scm 从 `fa4-v*` tag 生成；FA3 在 `hopper/` 内独立维护。

#### 4.3.4 代码实践

**实践目标**：亲手验证「FA2 排除 FA4」这件事，理解排除列表的意义。

**操作步骤**：

1. 打开根 [`setup.py`](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/setup.py#L760-L773)，定位到 `find_packages(...)` 的 `exclude=(...)`。
2. 思考实验：**如果删掉** exclude 里的 `"flash_attn.cute"` 和 `"flash_attn.cute.*"` 两项，会发生什么？
3. 用下面这段「思考实验」代码（示例代码，**不要真的去改 setup.py**）验证 `find_packages` 的行为：

```python
# 示例代码：模拟 find_packages 的排除效果（仅作演示，不修改项目）
from setuptools import find_packages

# 当前真实排除列表
real_exclude = ("build", "csrc", "include", "tests", "dist", "docs",
                "benchmarks", "flash_attn.egg-info",
                "flash_attn.cute", "flash_attn.cute.*")

# 假装去掉 cute 排除
fake_exclude = tuple(x for x in real_exclude if not x.startswith("flash_attn.cute"))

print("真实排除下抓到的包:", sorted(find_packages(exclude=real_exclude)))
print("去掉 cute 排除后  :", sorted(find_packages(exclude=fake_exclude)))
```

**需要观察的现象**：真实排除列表里**不应**出现 `flash_attn.cute`；而去掉排除后**会**出现 `flash_attn.cute`。

**预期结果**：去掉排除后，FA2 的 wheel 会把 FA4 的代码也打进去，与 FA4 自己的 wheel 内容重叠；当两个 wheel 都安装时，谁后装谁覆盖，造成难以排查的版本混乱。这正是排除列表存在的意义。

**说明**：本实践为「阅读 + 思考实验」型，无需 GPU；在仓库根目录运行即可，具体输出「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 FA4 的 `pyproject.toml` 里**没有** `CUDAExtension`，而 FA2 的 `setup.py` 里有？

> **答案**：FA4 是纯 Python + CuTeDSL，kernel 在运行时 JIT 编译成 PTX/CUBIN，安装阶段不需要 nvcc；FA2 是 C++/CUDA 源码，必须在 `pip install` 时用 nvcc 编译成 `.so` 扩展（`flash_attn_2_cuda`），所以需要 `CUDAExtension`。

**练习 2**：如果把 `flash_attn/__init__.py` 里的 `__path__ = extend_path(__path__, __name__)` 这行删掉，FA2 与 FA4 还能共存吗？

> **答案**：会出问题。没有 `extend_path`，`flash_attn` 就不是命名空间包，Python 只会从「最先安装的那个 `flash_attn/` 目录」找子模块，`flash_attn.cute` 可能就找不到（取决于谁后装、site-packages 里的目录结构）。`extend_path` 的作用就是把两个独立 wheel 贡献的路径合并，让 `flash_attn` + `flash_attn.cute` 同时可见。

**练习 3**：FA4 的 `pyproject.toml` 里 `setuptools_scm` 的 `root = "../.."` 是什么意思？为什么是两级上层？

> **答案**：`flash_attn/cute/pyproject.toml` 在 `flash_attn/cute/` 目录里，往上两级正好是仓库根；setuptools-scm 需要在仓库根读取 git tag（匹配 `fa4-v*`）来推导 FA4 的版本号，所以 `root` 指向 `../..`。

---

## 5. 综合实践

**任务**：绘制一张「仓库目录树」，用不同颜色（或不同前缀标记）标注 FA2、FA3、FA4 的代码位置，并为每个关键目录写一句话用途说明。这是把本讲三个最小模块串起来的总练习。

**操作步骤**：

1. 在仓库根目录运行只读命令，获取真实目录结构（不要凭记忆）：

```bash
git ls-files | cut -d/ -f1 | sort -u          # 一级目录
git ls-files 'flash_attn/*' | cut -d/ -f1-2 | sort -u   # flash_attn/ 下结构
git ls-files 'hopper/*.py'                              # FA3 的 Python 入口
git ls-files 'flash_attn/cute/*.py' | head              # FA4 文件
```

2. 用下面的标记规范整理成一棵树（示例骨架，**请用真实命令的输出填充**）：

```
flash-attention/
├── [FA2] flash_attn/            ← FA2 Python 接口包（flash-attn 2.8.4）
│   ├── __init__.py              ← extend_path 让 FA4 子包共存
│   ├── flash_attn_interface.py  ← FA2 的 flash_attn_func 定义
│   └── [FA4] cute/              ← FA4 子包（flash-attn-4，本手册主线）
│       ├── __init__.py          ← 导出 flash_attn_func / flash_attn_varlen_func
│       ├── interface.py         ← FA4 公共 API（后续讲义重点）
│       └── flash_fwd.py / flash_bwd.py / ...  ← FA4 kernel 源码
├── [FA2] csrc/                  ← FA2 的 C++/CUDA 源码（含 ROCm ck 后端、辅助算子）
│   ├── flash_attn/              ← FA2 CUDA 内核（被根 setup.py 编译）
│   ├── flash_attn_ck/           ← ROCm composable_kernel 后端
│   ├── cutlass/、composable_kernel/  ← git 子模块（CUTLASS / CK）
│   ├── fused_dense_lib/、layer_norm/ ← 辅助算子
├── [FA3] hopper/                ← FA3 全部代码（自带 setup.py，产出 flash_attn_3）
├── tests/                       ← 测试（tests/cute/ 给 FA4，其余给 FA2 生态）
├── benchmarks/                  ← FA2 风格基准脚本
├── training/                    ← GPT 端到端训练示例 + Hydra 配置
├── examples/inference/          ← 推理示例
├── AI/                          ← 调试笔记与复现脚本（非源码，lab notes）
├── setup.py                     ← FA2 构建入口（编译 csrc/flash_attn/）
├── README.md                    ← 项目总说明（分章介绍三代）
└── Makefile / .github/ / assets/ / ...
```

3. 给「每个关键目录」补一句话用途（可参考本讲 §3 的源码地图表）。

**需要观察的现象 / 预期结果**：你能清楚看到——FA2 占据 `flash_attn/`（顶层）+ `csrc/`；FA3 完全独立在 `hopper/`；FA4 嵌套在 `flash_attn/cute/` 内。三代不共享 kernel 源码，只共享 `flash_attn` 这个命名空间。

**说明**：本实践为纯文件系统阅读型，无需 GPU、无需安装任何包；目录树的具体内容以 `git ls-files` 实际输出为准，「待本地验证」。

## 6. 本讲小结

- 这个仓库**三代同堂**：FA2（顶层 `flash_attn/` + `csrc/`，C++/CUDA）、FA3（`hopper/`，C++/CUDA）、FA4（`flash_attn/cute/`，Python + CuTeDSL，本手册主线）。
- 三代面向不同 GPU、用不同语言、产出**三个独立的 wheel**：`flash-attn`、`flash-attn-3`、`flash-attn-4`。
- FA4 借 `flash_attn` 命名空间与 FA2 共存：靠 `flash_attn/__init__.py` 的 `pkgutil.extend_path`、FA2 `setup.py` 的 `find_packages(exclude=[..., "flash_attn.cute", ...])`、以及 FA4 `pyproject.toml` 的 `packages=["flash_attn.cute"]` 三者配合实现。
- FA2/FA3 在**安装时**用 nvcc 编译；FA4 在**运行时** JIT 编译，所以它的 `pyproject.toml` 没有 `CUDAExtension`。
- 测试、基准、训练、调试各有其位：`tests/cute/`（FA4 测试）、`benchmarks/`、`training/`、`examples/inference/`、`AI/`（调试笔记）。
- 看到同名 `flash_attn_func` 不要默认是同一个函数——它可能来自 FA2、FA3 或 FA4 三处之一。

## 7. 下一步学习建议

- 下一讲 [u1-l3（安装并第一次调用 FA4）] 会带你真正 `pip install flash-attn-4`、跑通第一个 `flash_attn_func(q,k,v,causal=True)`，把本讲的「地图」变成「能跑的代码」。
- 再下一讲 [u1-l4（FA2 与 FA4 接口对比与共存机制）] 会用脚本实证本讲 §4.3 的共存机制——验证两个 `flash_attn_func` 来自不同实现。
- 想提前熟悉 FA4 的公开 API，可以先扫一眼 [flash_attn/cute/interface.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py)，那是 [u2-l1（公共 API 详解）] 的主战场。
- 暂时不必进入 kernel 内部；先把「导航地图」背熟，后续讲义会自顶向下逐层深入。
