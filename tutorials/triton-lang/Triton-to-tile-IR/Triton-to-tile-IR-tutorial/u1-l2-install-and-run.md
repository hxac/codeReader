# 安装与运行方式

## 1. 本讲目标

承接 [u1-l1](u1-l1-project-overview.md)：你已经知道本仓库是「上游 Triton + 一个可开关的 CUDA Tile IR 后端」。本讲解决一个最实际的问题——**怎么把它装上、怎么在两个后端之间切换、怎么确认当前到底走的是哪一个后端**。

学完本讲后，你应当能够：

- 掌握**两种安装方式**：从源码 `pip install`（开发/构建安装），以及用 `nvtriton` wheel 与上游 `triton`（oait）**并存安装**；
- 学会用 **`PYTHONPATH` / `ENABLE_TILE`** 这两个环境变量，在 `nvtriton`（TileIR 后端）与 `oait`（PTX 后端）之间随时切换；
- 写出**两条验证命令**，分别确认「默认走 oait」与「开启 `ENABLE_TILE` 后 `driver.active` 为 `TileIRDriver`」。

> 说明：本仓库 README 自己有一句轻描淡写的话——"How to install? doesn't change"（安装方式没变）。这是因为从源码安装时，本仓库和上游 Triton 用的是同一套 `pip install` 流程，只是**装完之后多了一个开关**。所以本讲的重心会放在「两种安装方式的适用场景」和「那个开关怎么用」上。

## 2. 前置知识

- **`pip install` 与 `--target`**：`pip install` 默认把包装进 Python 的 `site-packages`（全局/虚拟环境的公共目录）。加上 `--target DIR` 后，包会被装进你指定的 `DIR` 目录，而**不碰** `site-packages`。本讲第二种安装方式就靠它实现「并存」。
- **`PYTHONPATH`**：Python 解释器在导入模块时，会**先**搜索 `PYTHONPATH` 列出的目录，**再**搜索 `site-packages`。所以如果把 `PYTHONPATH` 指向某个装有 `triton` 的目录，它就会「遮蔽（shadow）」`site-packages` 里的那个 `triton`。这是「并存切换」的核心原理。
- **`-e`（editable）安装**：`pip install -e .` 以「可编辑模式」安装——源码改了立即生效，适合在本仓库里开发调试。`pip install .`（不带 `-e`）则会真正构建并拷贝产物，适合「装好就用」。
- **`oait` 与 `nvtriton`**：本讲沿用 `INSTALL.md` 的术语。**oait** = OpenAI 上游 Triton（走 NVIDIA PTX 后端，默认）；**nvtriton** = 本仓库发布的 wheel，即「带 TileIR 后端的 Triton」。
- **`--no-deps`**：安装时跳过依赖。因为 `nvtriton` 与 `oait` 共享同一套依赖（PyTorch、torch 已经装好），并存安装时无需重复拉依赖。

> 一句话直觉：**方式一（源码安装）适合「我就是要在本仓库开发」；方式二（wheel 并存安装）适合「我已经有正常 Triton，只想临时切到 TileIR 试试，互不干扰」。**

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [INSTALL.md](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md) | **并存安装的权威文档**：讲解 `nvtriton` 与 `oait` 并存、用环境变量切换、验证 `driver.active`，以及 Docker 用法。本讲最小模块二、三的主要依据。 |
| [README.md](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md) | **仓库主说明**：给出源码安装命令 `pip install -e .` 与运行开关 `export ENABLE_TILE=1`。最小模块一的依据之一。 |
| [third_party/tileir/README.md](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/README.md) | **后端构建说明**：给出 `pip install .` 与「需先装 CUDA Toolkit 13.1（CTK 13.1）」的前提。最小模块一的依据之二。 |
| [scripts/install_nvtriton.sh](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/scripts/install_nvtriton.sh) | **自动化安装脚本**：把「下载 wheel + `--target` 安装 + 生成 activate/deactivate 脚本」一键封装。帮助你理解并存安装的完整流程。 |
| [python/triton/runtime/driver.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/driver.py) | **driver 选择逻辑**：`_create_driver()` 读取 `ENABLE_TILE`，决定返回 `TileIRDriver` 还是默认后端的 driver。这是验证命令 `driver.active` 背后的源码真相。 |

---

## 4. 核心概念与源码讲解

本讲围绕三个最小模块展开：**源码安装**、**wheel 并存安装**、**环境变量开关与验证**。

### 4.1 源码安装：从仓库直接安装

#### 4.1.1 概念说明

「源码安装」就是直接在本仓库根目录跑 `pip install`，把整个 Triton（连同 TileIR 后端）构建并安装到当前 Python 环境。这是上游 Triton 原本的安装方式，本仓库**没有改变它**——这就是 README 里那句 "doesn't change" 的含义。

源码安装又分两种细微差异：

- **`pip install -e .`（可编辑）**：仓库根目录的 [README.md](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md) 推荐这种。适合你就在本仓库里改代码、跑实验。
- **`pip install .`（非可编辑）**：`third_party/tileir/README.md` 给的命令。适合「构建一次、装好就用」。

两种都能让 TileIR 后端可用，区别只在「源码改动是否立即生效」。

#### 4.1.2 核心流程

源码安装的整体流程：

```
(0) 前提：已装 CUDA Toolkit 13.1（CTK 13.1），含 bin/tileiras、bin/ptxas、nvvm/lib64/libnvvm.so
       │
       ▼
(1) 克隆本仓库并进入根目录
       │
       ▼
(2) pip install -e .      ← 构建并安装 Triton（含 TileIR 后端）到当前环境
       │                     （C++ 插件在此阶段被编译、链接，详见 u4-l4）
       ▼
(3) export ENABLE_TILE=1  ← 打开后端开关，默认走 TileIR
       │
       ▼
(4) 运行你的 Triton 内核   ← 前端 → TTIR → TileIR → cubin（详见 u1-l4）
```

注意一个关键前提：`third_party/tileir/README.md` 明确要求**先装好 CTK 13.1**，再设 `ENABLE_TILE=1`。源码安装本身不会替你装 CUDA Toolkit。

#### 4.1.3 源码精读

仓库主说明给出的安装与运行方式（注意标题下那句 "doesn't change"）：

[README.md:42-52](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L42-L52) —— 安装用 `pip install -e .`；运行 TileIR 后端只需 `export ENABLE_TILE=1`。这是源码安装最直接的两条命令。

后端构建说明补充的前提与命令：

[third_party/tileir/README.md:3-17](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/README.md#L3-L17) —— 强调 `pip install .`（非可编辑），并写明「Before using the backend, ensure you have CTK 13.1 installed」，然后 `export ENABLE_TILE=1`。

> 名词解释：
> - **CTK（CUDA Toolkit）13.1**：NVIDIA 提供的 CUDA 13.1 工具链。TileIR 后端依赖其中的 `tileiras`（把 Tile IR bytecode 编译成 cubin 的工具）、`ptxas`、`libnvvm.so`。详见 [README.md:94-98](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L94-L98)。

#### 4.1.4 代码实践

这是一个**命令操作型实践**（**待本地验证，需要 Blackwell GPU + CTK 13.1 + Python 3.12/3.13 虚拟环境**）。

1. **实践目标**：用源码安装方式把带 TileIR 后端的 Triton 装进一个干净虚拟环境，并打开后端开关。
2. **操作步骤**：
   ```bash
   # 1) 准备一个干净的虚拟环境（Python 3.12 或 3.13），并装好 PyTorch
   python -m venv venv && source venv/bin/activate
   # 2) 进入仓库根目录
   cd Triton-to-tile-IR
   # 3) 可编辑安装（构建 C++ 插件，首次较慢）
   pip install -e .
   # 4) 打开 TileIR 后端开关
   export ENABLE_TILE=1
   ```
3. **需要观察的现象**：`pip install -e .` 过程中会编译 C++ 部分（包含 TileIR 的 MLIR 转换插件），首次构建耗时较长。
4. **预期结果**：安装成功后，`import triton` 不报错；在 `ENABLE_TILE=1` 下运行内核时走 TileIR 后端。
5. **无法运行时**：若没有 Blackwell GPU / CTK 13.1，可只做「源码阅读型实践」——阅读 [README.md:42-52](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L42-L52) 与 [third_party/tileir/README.md:3-17](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/README.md#L3-L17)，对比 `pip install -e .` 与 `pip install .` 的差异，说明各自适合什么场景。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 说源码安装 "doesn't change"？

> **参考答案**：因为从源码安装时，本仓库沿用了上游 Triton 完全相同的 `pip install` 流程；TileIR 后端是「新增但可开关」的路径，安装命令本身不变，只是装完之后多了 `ENABLE_TILE=1` 这个运行期开关。

**练习 2**：`pip install -e .` 与 `pip install .` 的区别是什么？在本仓库里分别适合谁？

> **参考答案**：`-e` 是可编辑安装，源码改动立即生效，适合在本仓库里开发/调试 TileIR 代码（仓库 README 推荐这种）；不带 `-e` 是常规安装，会真正构建并拷贝产物，适合「装好就用、不再改源码」（`third_party/tileir/README.md` 给出的就是这种）。两者都能启用 TileIR 后端。

---

### 4.2 wheel 并存安装：nvtriton 与 oait 共存

#### 4.2.1 概念说明

源码安装有一个局限：它把 Triton 装进 `site-packages`，会**替换/占用**原本的 Triton。如果你已经有一个正常运行的 oait（比如随 PyTorch 装好的 `triton==3.6.0`），又想**临时**试试 TileIR 后端，却不想破坏原有环境，就需要「并存安装」。

这就是 `INSTALL.md` 解决的核心场景：把 `nvtriton`（带 TileIR 的 wheel）装进一个**隔离目录**，让两个 `triton` 在磁盘上互不干扰，再用 `PYTHONPATH` 决定运行时用哪一个。

关键设计有三个：

- **隔离目录 `--target`**：`nvtriton` 不进 `site-packages`，而是装到 `$NVTRITON_DIR`。
- **`--no-deps`**：因为依赖和 oait 相同（PyTorch 已在），不必重复装。
- **`PYTHONPATH` 遮蔽**：运行时把 `PYTHONPATH` 指向隔离目录，即可临时切到 `nvtriton`；取消设置即回到 oait。

> 名词解释：
> - **wheel（`.whl`）**：Python 的预编译分发包。`nvtriton-3.6.0-cp312-cp312-linux_x86_64.whl` 中，`cp312` 表示适配 Python 3.12、`linux_x86_64` 表示适配 x86-64 Linux。

#### 4.2.2 核心流程

并存安装与切换的完整流程：

```
┌──────────────────────────────────────────────────────────────┐
│  site-packages/  ──► oait triton（默认，随 PyTorch 装好）      │
│  $NVTRITON_DIR/  ──► nvtriton（带 TileIR，隔离目录，独立安装）  │
└──────────────────────────────────────────────────────────────┘

默认（什么都不设）：
   import triton  ──► site-packages 里的 oait

切到 TileIR：
   PYTHONPATH=$NVTRITON_DIR ENABLE_TILE=1 python ...
        │                  │
        │                  └─► 打开后端开关（决定 driver 选谁）
        └─► 让 Python 先找 $NVTRITON_DIR，遮蔽 site-packages 的 oait

切回 oait：
   unset PYTHONPATH ENABLE_TILE
```

`INSTALL.md` 还解释了为什么这套机制能成立——`PYTHONPATH` 的搜索顺序优先于 `site-packages`：

```
Python 导入模块的搜索顺序：
   1. PYTHONPATH 列出的目录        ◄── 指向 $NVTRITON_DIR 时，先找到 nvtriton 的 triton/
   2. site-packages                ◄── fallback 回 oait 的 triton/
```

#### 4.2.3 源码精读

并存安装的「三步走」命令：

[INSTALL.md:14-26](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md#L14-L26) —— 定义隔离目录 `NVTRITON_DIR`，`mkdir` 创建它，再用 `pip install --no-cache-dir --no-deps --target $NVTRITON_DIR ./nvtriton-...whl` 把 wheel 装进去。`INSTALL.md` 明确说明 `--no-deps` 是必需的，因为依赖已和 oait 共享。

切换与还原的命令：

[INSTALL.md:37-43](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md#L37-L43) —— 切到 nvtriton：`PYTHONPATH=$NVTRITON_DIR ENABLE_TILE=1 python my_script.py`（单次），或 `export` 写进当前 shell 会话。

[INSTALL.md:56-63](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md#L56-L63) —— 切回 oait：`unset PYTHONPATH ENABLE_TILE` 即可（或开新 shell）。

原理说明（PYTHONPATH 遮蔽）：

[INSTALL.md:83-88](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md#L83-L88) —— 「`PYTHONPATH` entries are searched before `site-packages`. When set to `$NVTRITON_DIR`, Python finds `$NVTRITON_DIR/triton/` first, which shadows the oait `triton/` in site-packages.」这是整个并存机制的原理基石。

仓库还提供了一个**一键脚本** `install_nvtriton.sh`，把上述流程封装起来，并自动探测 Python 版本来选 wheel：

[scripts/install_nvtriton.sh:13-28](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/scripts/install_nvtriton.sh#L13-L28) —— 从 release URL 下载对应 `cp` tag 的 wheel，再用 `pip install --no-cache-dir --no-deps --target "$INSTALL_DIR"` 装进隔离目录。其中 `PY_TAG` 由 `sys.version_info` 自动算出（如 `cp312`）。

脚本还会自动生成 `activate.sh` / `deactivate.sh`，把 `PYTHONPATH`/`ENABLE_TILE`/`HELION_BACKEND` 的设置/还原封装成 `source` 即可：

[scripts/install_nvtriton.sh:30-51](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/scripts/install_nvtriton.sh#L30-L51) —— `activate.sh` 里 `export PYTHONPATH`、`ENABLE_TILE=1`、`HELION_BACKEND=tileir`；`deactivate.sh` 里把 `PYTHONPATH` 中属于本目录的项过滤掉并 `unset` 那两个开关。这等价于 `INSTALL.md` 的手动命令，只是封装得更省事。

一个很实用的细节：`nvtriton` wheel **自带** `tileiras`/`ptxas` 二进制，不必单独装 CUDA Toolkit：

[INSTALL.md:114-122](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md#L114-L122) —— "The nvtriton wheel embeds `tileiras` and `ptxas` binaries in `triton/backends/tileir/scripts/cuda_dep_x86/`. No separate CUDA toolkit is needed for the TileIR backend to function." 这意味着**并存安装（方式二）比源码安装（方式一）省去了自行配置 CTK 13.1 的麻烦**——这也是为什么「只想试试」的用户更推荐 wheel 方式。

> 小结两种方式的 CTK 依赖差异：
>
> | 安装方式 | 是否需要自己装 CTK 13.1 | 二进制来源 |
> |---|---|---|
> | 源码安装（4.1） | 需要（`third_party/tileir/README.md` 要求） | 系统的 `tileiras`/`ptxas`/`libnvvm.so` |
> | wheel 并存安装（4.2） | 不需要 | wheel 内嵌的 `cuda_dep_x86/` 二进制 |

#### 4.2.4 代码实践

这是一个**源码阅读 + 命令梳理型实践**（命令部分**待本地验证**，阅读部分无需环境）。

1. **实践目标**：搞清楚并存安装「为什么要 `--no-deps`、为什么要 `--target`、为什么靠 `PYTHONPATH` 切换」。
2. **操作步骤**：
   - 阅读 [INSTALL.md:14-26](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md#L14-L26) 的安装命令，找到 `--no-deps`、`--target`、`NVTRITON_DIR` 三个要素。
   - 阅读 [INSTALL.md:83-88](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md#L83-L88)，理解 `PYTHONPATH` 的遮蔽原理。
   - 对比手动命令（`INSTALL.md`）与一键脚本（`install_nvtriton.sh`）。
3. **需要观察的现象**：你会发现脚本做的事和文档里的手动命令**一一对应**——下载 wheel → `--target` 安装 → 写出 activate/deactivate。
4. **预期结果**：能口述「并存安装三要素」：隔离目录（`--target`）、跳过依赖（`--no-deps`）、运行期切换（`PYTHONPATH`）。
5. **若本地可联网且有 Python 3.12/3.13 环境**（**待本地验证**）：可直接跑 `bash scripts/install_nvtriton.sh`，它会装到 `~/.local/triton_tileir`，然后 `source ~/.local/triton_tileir/activate.sh` 切换；用完 `source .../deactivate.sh` 切回。卸载用 `scripts/uninstall_nvtriton.sh`（注意它会在 `ENABLE_TILE`/`PYTHONPATH` 仍激活时报错拒绝删除，见 [scripts/uninstall_nvtriton.sh:12-16](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/scripts/uninstall_nvtriton.sh#L12-L16)，这是一种安全保护）。

#### 4.2.5 小练习与答案

**练习 1**：并存安装时为什么必须用 `--no-deps`？

> **参考答案**：因为 `nvtriton` 与 oait 共享完全相同的依赖（如 PyTorch 等，这些已经随 oait 装好）。如果不用 `--no-deps`，pip 会把依赖也拷进 `--target` 目录，既冗余又可能与 `site-packages` 里的依赖冲突。`INSTALL.md:24-25` 明确写了这一点。

**练习 2**：为什么并存安装能「互不干扰」？切回 oait 需要重装吗？

> **参考答案**：不需要重装。因为 `nvtriton` 装在隔离目录里，**从未触碰** `site-packages` 中的 oait。切换只是改变 Python 的导入搜索路径（`PYTHONPATH`）：设置时优先找到 `nvtriton`，`unset` 后 Python 自然 fallback 回 `site-packages` 的 oait。两套安装是磁盘上完全独立的两份。

---

### 4.3 环境变量开关与验证：在两后端间切换

#### 4.3.1 概念说明

装好之后，最关键的问题是：**怎么确认我现在到底走的是哪个后端？** 光设了 `ENABLE_TILE=1` 不够，还要确认 Python 真的加载了 `nvtriton`、且 driver 真的选成了 `TileIRDriver`。

这里有两个层次要区分：

- **加载了哪个 `triton` 包**（`triton.__file__`）：决定走 oait 还是 nvtriton 的代码。靠 `PYTHONPATH` 控制。
- **激活了哪个 driver**（`driver.active`）：决定编译/启动走哪条后端路径。靠 `ENABLE_TILE` 控制。

二者要配合：必须**既**让 `PYTHONPATH` 指向 `nvtriton`，**又**设 `ENABLE_TILE=1`，才会真正启用 TileIR 后端。只设其中一个都不对。

#### 4.3.2 核心流程

验证当前后端的决策树：

```
        python -c "import triton; print(triton.__file__)"
                    │
   ┌────────────────┴────────────────┐
   ▼                                 ▼
 路径含 site-packages           路径含 $NVTRITON_DIR
 = 当前走 oait（PTX 后端）       = 当前走 nvtriton 包
   │                                 │
   │                  再看 ENABLE_TILE 是否=1？
   │                                 ├─ 否 ─► 即使是 nvtriton 包，也走默认后端
   │                                 └─ 是 ─► driver.active 为 TileIRDriver ✓
   ▼
（默认 PTX 后端，driver.active 为 NVIDIA driver）
```

最终的金标准验证（`INSTALL.md` 给的命令）：

```bash
python -c "from triton.runtime.driver import driver; print(type(driver.active).__name__)"
# 在 PYTHONPATH=$NVTRITON_DIR ENABLE_TILE=1 下 → 输出 TileIRDriver
```

#### 4.3.3 源码精读

要理解 `driver.active` 为什么会变成 `TileIRDriver`，必须看 `python/triton/runtime/driver.py`。整个选择逻辑就在 `_create_driver()` 里：

[python/triton/runtime/driver.py:8-12](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/driver.py#L8-L12) —— 当 `ENABLE_TILE == "1"` 时，**直接强制** `from ..backends.tileir.driver import TileIRDriver; return TileIRDriver()`。注意这是 `if` 在最前面、优先级最高的分支：开了开关就**无条件**用 TileIRDriver，绕过下面的自动选择逻辑。

[python/triton/runtime/driver.py:14-27](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/driver.py#L14-L27) —— 否则（未设或 `ENABLE_TILE != "1"`）走自动选择：先看 `TRITON_DEFAULT_BACKEND`，再从「active drivers」里挑。这就是默认走 oait（PTX）后端的路径。

`driver.active` 到底是什么？它是一个惰性属性：

[python/triton/runtime/driver.py:36-46](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/driver.py#L36-L46) —— `default` 属性在首次访问时调用 `_create_driver()` 实例化一个 driver；`active` 属性在未显式设置时直接复用 `default`。所以 `driver.active` 首次被读时，才会触发上面那段 `ENABLE_TILE` 判断。

[python/triton/runtime/driver.py:55](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/driver.py#L55) —— 模块级 `driver = DriverConfig()` 就是验证命令里 `from triton.runtime.driver import driver` 导入的那个对象。

`TileIRDriver` 本身定义在 TileIR 后端代码里（注意：这条 import 路径 `..backends.tileir.driver` 只有装了 `nvtriton`/本仓库后才存在）：

[third_party/tileir/backend/driver.py:548-552](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L548-L552) —— `class TileIRDriver(GPUDriver)`，构造时挂上 `TileIRUtils` 工具模块与 `TileIRLauncher` 启动器类。这正是验证命令输出 `TileIRDriver` 的那个类。

`TileIRDriver` 还有一个 `is_active()` 静态方法，把 `ENABLE_TILE` 也作为「是否激活」的判据之一：

[third_party/tileir/backend/driver.py:571-582](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/driver.py#L571-L582) —— 只有 `torch.cuda.is_available()` 且 `ENABLE_TILE == "1"` 且非 HIP 平台时，`TileIRDriver.is_active()` 才返回 `True`。这说明 `ENABLE_TILE` 在「自动选择」分支里也扮演判据角色；而 `_create_driver` 因为把它放在最优先的 `if`，所以**开启开关时一定会被选中**。

最后，`INSTALL.md` 把这一切凝练成三条验证命令：

[INSTALL.md:65-81](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md#L65-L81) —— 三条命令分别验证「默认是 oait」「PYTHONPATH 指向 nvtriton」「`driver.active` 是 `TileIRDriver`」。

> 一个容易被忽略的点：`ENABLE_TILE` 必须在 `import triton`（或 `from triton.runtime.driver import driver`）**之前**设置，因为 driver 是在首次访问 `driver.active`/`driver.default` 时一次性创建并缓存的。这也正是 README 提醒 Helion 用户「必须在 `import helion/triton` 之前设 `os.environ`」的同一类原因（[README.md:9](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/README.md#L9)）。在 shell 里用 `ENABLE_TILE=1 python ...` 的前置写法天然满足这个顺序。

#### 4.3.4 代码实践

这是本讲的**核心实践**，对应规格中的任务：在隔离目录安装 nvtriton wheel，并写出两条 shell 命令——一条确认默认走 oait，一条开启 `ENABLE_TILE` 后确认 `driver.active` 为 `TileIRDriver`。

1. **实践目标**：亲手完成「并存安装 → 默认验证 → 切换验证」全流程，确认两个后端都能被正确选中。
2. **操作步骤（待本地验证，需要 Blackwell GPU + Python 3.12/3.13 venv）**：

   ```bash
   # (A) 在隔离目录安装 nvtriton wheel（oait 不受影响）
   NVTRITON_DIR=$VIRTUAL_ENV/opt/nvtriton
   mkdir -p $NVTRITON_DIR
   pip install --no-cache-dir --no-deps --target $NVTRITON_DIR \
       ./nvtriton-3.6.0-cp312-cp312-linux_x86_64.whl
   ```

3. **写出两条验证命令**（这正是任务要求的输出）：

   **第一条——确认默认走 oait**（来自 [INSTALL.md:67-70](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md#L67-L70)）：
   ```bash
   python -c "import triton; print(triton.__file__)"
   # 预期：.../site-packages/triton/__init__.py   ← 说明默认是 oait
   ```

   **第二条——开启 ENABLE_TILE 后确认 driver.active 为 TileIRDriver**（来自 [INSTALL.md:77-80](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md#L77-L80)）：
   ```bash
   PYTHONPATH=$NVTRITON_DIR ENABLE_TILE=1 \
     python -c "from triton.runtime.driver import driver; print(type(driver.active).__name__)"
   # 预期：TileIRDriver   ← 说明已切到 TileIR 后端
   ```

4. **需要观察的现象**：
   - 第一条命令的路径里含 `site-packages`，说明没设 `PYTHONPATH` 时加载的是 oait；
   - 第二条命令在同时设了 `PYTHONPATH`（加载 nvtriton）和 `ENABLE_TILE=1`（强制选 TileIRDriver）后，输出 `TileIRDriver`。
5. **预期结果**：两条命令分别输出 `site-packages/.../triton/__init__.py` 和 `TileIRDriver`，证明「默认 oait / 可切换 TileIR」并存成立。
6. **无法运行时（纯源码阅读，无需 GPU）**：对照 [python/triton/runtime/driver.py:8-12](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/driver.py#L8-L12) 解释：为什么第二条命令会输出 `TileIRDriver`？答案就是 `_create_driver()` 把 `ENABLE_TILE == "1"` 的判断放在最优先的 `if`，直接 `return TileIRDriver()`，而 `driver.active` 首次被读时会触发这个判断。你还可以追问：如果**只设 `ENABLE_TILE=1` 但不设 `PYTHONPATH`** 会怎样？（见下面练习 2）

#### 4.3.5 小练习与答案

**练习 1**：验证后端时为什么要同时看 `triton.__file__` 和 `driver.active` 两个东西？

> **参考答案**：因为它们验证的是两个不同层次。`triton.__file__` 验证「加载了哪个 triton 包」（oait 还是 nvtriton，由 `PYTHONPATH` 控制）；`driver.active` 验证「激活了哪个后端 driver」（PTX 还是 TileIRDriver，由 `ENABLE_TILE` 控制）。两者必须同时满足才能真正启用 TileIR 后端，所以要分开确认。

**练习 2**：如果只设 `ENABLE_TILE=1`、但**不设** `PYTHONPATH`（即仍加载 site-packages 的 oait），会启用 TileIR 后端吗？

> **参考答案**：不会。因为 `_create_driver()` 里 `from ..backends.tileir.driver import TileIRDriver`（[python/triton/runtime/driver.py:11](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/driver.py#L11)）这条 import 路径只有在装了 `nvtriton`/本仓库后才存在。oait 的 `site-packages` 里没有 `backends.tileir` 这个子包，import 会失败。所以必须**先**用 `PYTHONPATH` 让 Python 加载到带 TileIR 的 `triton`，`ENABLE_TILE=1` 才有意义。这也是 `INSTALL.md` 切换命令总是 `PYTHONPATH=$NVTRITON_DIR ENABLE_TILE=1` 两个一起设的原因。

**练习 3**：为什么 `ENABLE_TILE` 必须在 `import triton` 之前设置？

> **参考答案**：因为 `driver` 对象（[python/triton/runtime/driver.py:55](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/driver.py#L55)）的 `active`/`default` 是**惰性缓存**属性：首次访问时调用 `_create_driver()` 创建一次并缓存（[python/triton/runtime/driver.py:36-46](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/driver.py#L36-L46)）。如果 `import triton` 触发了首次访问时 `ENABLE_TILE` 还没设，driver 就会被创建成默认后端并缓存住，之后再设开关也来不及了。所以要在 import 之前设置——shell 的 `ENABLE_TILE=1 python ...` 写法正好保证环境变量在进程启动时就已就位。

---

## 5. 综合实践

把三个最小模块串起来，完成一个「**双后端并存最小实验**」。

假设你是团队的 GPU 算子工程师，环境里已经有一个随 PyTorch 装好的 oait（`triton==3.6.0`，走 PTX 后端），现在你想在不破坏它的前提下，临时验证某个内核在 TileIR 后端下的行为。请完成以下任务，并把每一步的**预期输出**写出来：

1. **并存安装**（对应 4.2）：参照 [INSTALL.md:14-26](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md#L14-L26) 或直接 `bash scripts/install_nvtriton.sh`，把 nvtriton wheel 装进隔离目录。说明你用了哪三个关键参数（`--target`、`--no-deps`、隔离目录路径），以及为什么 oait 不会受影响（依据 [INSTALL.md:83-88](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md#L83-L88)）。

2. **默认验证**（对应 4.3）：写一条命令确认默认仍是 oait（依据 [INSTALL.md:67-70](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md#L67-L70)），写出预期输出。

3. **切换验证**（对应 4.3）：写一条命令切到 TileIR 后端并确认 `driver.active` 为 `TileIRDriver`（依据 [INSTALL.md:77-80](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md#L77-L80)），写出预期输出。

4. **原理解释**（对应 4.1 + 4.3）：用一句话解释为什么源码安装（`pip install -e .`）和 wheel 并存安装（`--target`）在「是否需要自带 CTK 13.1」上不同（依据 [INSTALL.md:114-118](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md#L114-L118) 与 [third_party/tileir/README.md:13-17](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/README.md#L13-L17)）。

5. **还原**：写一条命令切回 oait（依据 [INSTALL.md:56-63](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/INSTALL.md#L56-L63)）。

完成这个实验，你就把「源码安装 / wheel 并存 / 环境变量切换与验证」三个模块真正跑通了。命令部分**待本地验证（需 Blackwell GPU + CTK 13.1 或 wheel 自带二进制）**；即使不跑，把 1–5 的命令与依据写清楚，也已达成学习目标。

## 6. 本讲小结

- **两种安装方式**：源码安装（`pip install -e .` 或 `pip install .`，适合在本仓库开发）与 wheel 并存安装（`pip install --no-deps --target DIR ./nvtriton-...whl`，适合临时试 TileIR 而不破坏原有 oait）。
- **并存三要素**：隔离目录（`--target`）、跳过依赖（`--no-deps`）、运行期切换（`PYTHONPATH`）。其中 `PYTHONPATH` 的搜索顺序优先于 `site-packages`，这是「遮蔽」oait 的原理。
- **后端开关**：`ENABLE_TILE=1` 打开 TileIR 后端；默认（未设或 `0`）走 oait 的 PTX 后端。
- **driver 选择的源码真相**：[python/triton/runtime/driver.py:8-12](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/runtime/driver.py#L8-L12) 把 `ENABLE_TILE == "1"` 放在最优先分支，无条件 `return TileIRDriver()`；`driver.active` 首次访问时惰性创建并缓存。
- **验证金标准**：`triton.__file__`（哪个包）+ `type(driver.active).__name__`（哪个后端）两条命令，分别确认加载层与 driver 层。
- **CTK 依赖差异**：源码安装需自行准备 CTK 13.1；wheel 并存安装则因 wheel 内嵌 `tileiras`/`ptxas` 而无需单独装 CUDA Toolkit。

## 7. 下一步学习建议

本讲解决了「怎么装、怎么切换、怎么验证」。建议按以下顺序继续：

1. **摸清代码在哪**：学习 [u1-l3 目录结构与代码组织](u1-l3-repo-structure.md)，识别 TileIR 后端的 Python / C++ / 测试 / 构建代码各在哪些路径——你会更清楚本讲里 `backends/tileir`、`scripts/` 这些目录在整体结构中的位置。
2. **建立端到端视图**：学习 [u1-l4 端到端编译链路总览](u1-l4-e2e-pipeline-overview.md)，把 `make_ttir` → `make_tileir` → `make_cubin` → 启动 的完整数据流串起来，理解 `ENABLE_TILE` 打开后内核是怎么一路编到 cubin 的。
3. **深入后端选择机制**：学完 u1-l3、u1-l4 后，进入 [u2-l1 后端选择机制](u2-l1-backend-selection.md)，从 `_create_driver` 与 `compiler.compile()` 的角度，彻底弄清「环境变量 → driver 实例 → target.backend」的完整决策链。

继续阅读的源码：本讲只触及 driver 的选择入口；若想提前看编译侧如何感知 `ENABLE_TILE`，可读 [python/triton/compiler/compiler.py:78-84](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py#L78-L84)（`make_ir` 中据开关选择不同的 `ast_to_ttir` 前端）。
