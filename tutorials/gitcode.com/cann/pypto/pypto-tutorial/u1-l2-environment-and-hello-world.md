# 环境搭建、编译安装与运行第一个算子

## 1. 本讲目标

学完本讲，你应当能够：

1. 根据自己的硬件条件（有无昇腾 NPU），选择合适的 PyPTO 环境搭建方式：CANNLab、Docker 或手动安装。
2. 完成 PyPTO 依赖安装，并通过 `build_ci.py` 从源码编译、安装 PyPTO 软件包。
3. 运行 `examples/00_hello_world/hello_world.py`，跑通第一个 `add` 算子。
4. 读懂这个示例里的三个关键语法：`@pypto.jit` 装饰器、`Tensor[...]` 类型标注、`out[:] = ...` 写回语法。
5. 区分 `RunMode.NPU`（真机执行）与 `RunMode.SIM`（仿真执行）两种运行模式，知道没有真机时如何用 SIM 模式学习。

## 2. 前置知识

本讲是动手课，先解释几个会反复出现的名词：

- **NPU（神经网络处理器）**：华为昇腾系列的 AI 加速芯片。PyPTO 编写的算子最终在 NPU 的 AI Core 上执行。
- **驱动与固件（HDK）**：让操作系统能"看见"并使用 NPU 硬件的底层软件。可以用 `npu-smi info` 命令检查它是否安装正常。
- **CANN**：昇腾异构计算架构，是华为提供的软件栈总称。PyPTO 是 CANN 生态中的一员，它编译出来的产物要依赖 CANN 的工具链（`Ascend-cann-toolkit`）和算子运行时（`Ascend-cann-ops`）。
- **编译态 / 运行态**：官方文档中的术语。"编译态"指只编译 PyPTO 源码、不在设备上运行，此时只需要 CANN toolkit；"运行态"指真正把算子跑起来，还需要驱动固件和 CANN ops 包。
- **run 包 / whl 包**：whl 是 Python 标准的wheel 安装包；run 包是华为 CANN 生态常用的自解压安装脚本（`.run` 文件），PyPTO 的源码编译产物就是一个 run 包，内部包含 whl 与 C++ 编译产物。
- **PyTorch 与 TorchNPU**：PyPTO 的示例用 PyTorch 准备输入数据、校验结果；`torch_npu` 是让 PyTorch 能使用 NPU 设备的适配插件。SIM（仿真）模式下不需要 `torch_npu`。

上一讲（u1-l1）我们已经建立了整体认知：PyPTO 程序会经历「Python 前端 → PIL → IR → 多层图 Pass → CodeGen → 设备执行」的编译链路。本讲不动源码 internals，只解决"把环境跑起来"这件事，为后续所有讲义提供可复现的实验环境。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用途 |
| --- | --- | --- |
| `examples/00_hello_world/hello_world.py` | 官方入门示例：一个向量加法算子 | 逐行精读，理解 jit / Tensor[...] / out[:] / 运行模式 |
| `docs/zh/install/prepare_environment.md` | 环境准备官方文档 | 三种搭建方式、依赖清单、环境验证 |
| `docs/zh/install/build_and_install.md` | PyPTO 编译安装官方文档 | 源码下载、run 包编译与安装命令 |
| `docs/zh/invocation/examples_invocation.md` | 样例运行官方文档 | SIM/NPU 两种模式的启动命令、产物查看 |
| `python/requirements.txt`（辅助） | Python 依赖清单 | 看清 pip 依赖分了哪几类 |
| `build_ci.py`（辅助） | 项目构建总入口脚本 | 理解 `--clean --no_isolation` 等参数 |
| `python/pypto/runtime.py`（辅助） | 运行时模块 | 确认 `RunMode` 枚举的真实定义 |
| `python/pypto/__init__.py`（辅助） | pypto 包的导出入口 | 确认 `jit`、`Tensor`、`RunMode` 从哪里来 |

## 4. 核心概念与源码讲解

### 4.1 环境准备：三种搭建方式与依赖清单

#### 4.1.1 概念说明

PyPTO 是编译型框架：Python 侧代码要被编译成 C++ 产物，再依赖 CANN 工具链接到设备运行时。因此"搭环境"实际包含三层内容：

1. **硬件与驱动层**：NPU 设备 + 驱动固件（没有真机可以用 CANNLab 云环境或 SIM 仿真绕开）。
2. **CANN 软件层**：`Ascend-cann-toolkit`（编译必需）+ `Ascend-cann-ops`（运行必需）。
3. **Python / 编译工具层**：Python ≥ 3.9、pip 依赖、cmake、gcc/g++ 等。

官方文档把这三种用户的诉求整理成了一张选择表。

#### 4.1.2 核心流程

环境准备的整体决策流程：

```text
你有昇腾设备吗？
├── 没有 ──→ 方式1：CANNLab 云开发环境（浏览器里直接用，免安装）
└── 有 ────→ 想快速搭好？
             ├── 是 ──→ 方式2：Docker 镜像（预集成 CANN，开箱即用）
             └── 否 ──→ 方式3：手动安装（驱动固件 + CANN 包 + PyPTO 依赖）
```

选定方式后，统一收尾动作是：

1. 安装 Python 依赖：`python3 -m pip install -r python/requirements.txt`。
2. 安装 PyTorch / TorchNPU（注意：必须先装完 CANN toolkit 再装 TorchNPU）。
3. 验证环境：`npu-smi info` 看驱动，`cat .../ascend*install.info` 看 CANN 版本。
4. `source .../set_env.sh` 让 CANN 环境变量在当前终端生效。

#### 4.1.3 源码精读

先看官方给出的三种方式对照表：

> [docs/zh/install/prepare_environment.md:14-18](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/install/prepare_environment.md#L14-L18) —— 官方把环境搭建分为 CANNLab（无设备）、Docker（有设备、求快）、手动安装（有设备、求新/求灵活）三条路径，并区分了"编译态"与"运行态"两种依赖范围。

**方式 1：CANNLab（无真机首选）**

> [docs/zh/install/prepare_environment.md:20-32](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/install/prepare_environment.md#L20-L32) —— 在开源项目页面点 `CANNLab` 按钮、用华为云账号登录，即可获得一个预装好驱动、软件包和依赖的在线昇腾环境，进入 WebIDE 就能写代码。

**方式 2：Docker**

> [docs/zh/install/prepare_environment.md:54-65](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/install/prepare_environment.md#L54-L65) —— 从昇腾镜像仓库拉取预集成 CANN 的镜像，再用 `docker run` 把宿主机的 `/dev/davinci0`、`/dev/davinci_manager`、`/dev/devmm_svm`、`/dev/hisi_hdc` 等设备文件和驱动库挂载进容器，容器内即可使用真实 NPU。

注意这条命令里 `--device /dev/davinci0` 的编号要按 `npu-smi info` 显示的实际卡号调整。

**方式 3：手动安装**

> [docs/zh/install/prepare_environment.md:104-120](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/install/prepare_environment.md#L104-L120) —— 手动安装 CANN 的两个包：`Ascend-cann-toolkit`（编译态就要装）和 `Ascend-cann-${soc_name}-ops`（运行态才需要），两者需安装到相同路径。

> [docs/zh/install/prepare_environment.md:99-102](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/install/prepare_environment.md#L99-L102) —— 驱动固件有版本下限：Ascend HDK 25.5.1 及以上。低于该版本不在验证范围内，可能出现 AIC 超时等异常。

**PyPTO 自己的依赖**

> [docs/zh/install/prepare_environment.md:133-163](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/install/prepare_environment.md#L133-L163) —— Python ≥ 3.9（要有 python3-dev 开发头文件）、pip 依赖、PyTorch/TorchNPU（强调三者 Python 版本要一致），以及 cmake ≥ 3.16.3、g++/gcc ≥ 7.3.1、pybind11 ≥ 2.13.6。

pip 依赖的实际清单在 `python/requirements.txt` 里，按用途分了组：

> [python/requirements.txt:1-27](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/requirements.txt#L1-L27) —— 依赖分为：基础编译（setuptools、pybind11）、`build_ci.py` 所需（pip、build、packaging）、UT/ST 测试（pytest 系列）、pypto 运行（PyYAML）、泳道图绘制（matplotlib、pandas、plotly）、精度对比脚本（tabulate）。

**环境验证与收尾**

> [docs/zh/install/prepare_environment.md:165-183](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/install/prepare_environment.md#L165-L183) —— 用 `npu-smi info` 验证驱动正常，用 `cat /usr/local/Ascend/cann/${arch}-linux/ascend*install.info` 查看 CANN 版本。

> [docs/zh/install/prepare_environment.md:227-237](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/install/prepare_environment.md#L227-L237) —— 最后 `source /usr/local/Ascend/cann/set_env.sh`（默认路径）让环境变量生效，可写入 `.bashrc`。

小提醒（诚实标注）：`prepare_environment.md` 给出的路径是 `/usr/local/Ascend/cann/set_env.sh`，而下一节样例运行文档给的是 `/usr/local/Ascend/ascend-toolkit/set_env.sh`，两份文档路径不一致，以你机器上的实际安装目录为准。

#### 4.1.4 代码实践

1. **实践目标**：确认本机满足 PyPTO 的前置条件，并装好 Python 依赖。
2. **操作步骤**：
   - `python3 --version`：确认 ≥ 3.9。
   - 有 NPU 的机器：`npu-smi info` 确认驱动正常；`cat /usr/local/Ascend/cann/$(uname -m)-linux/ascend*install.info` 查看 CANN 版本。
   - 进入仓库根目录，执行 `python3 -m pip install -r python/requirements.txt`。
3. **需要观察的现象**：pip 安装过程中出现了哪些包；`npu-smi info` 是否输出设备表格。
4. **预期结果**：pip 全部安装成功；有 NPU 时能看到设备信息。无 NPU 机器上 `npu-smi info` 不可用属正常现象，请改用 CANNLab 或后续 SIM 模式。本实践涉及本机环境，具体输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：只想编译 PyPTO、不运行，需要安装 CANN ops 包和驱动固件吗？
答案：不需要。文档明确区分了编译态与运行态：ops 包与驱动固件都是运行态依赖，编译态只需 CANN toolkit 包及编译工具。

**练习 2**：`python/requirements.txt` 里哪几个依赖是"绘制泳道图"用的？
答案：`matplotlib`、`pandas`、`plotly`（见 [python/requirements.txt:21-24](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/requirements.txt#L21-L24)），泳道图是查看算子执行时间线的产物，第 4.3 节会再提到。

**练习 3**：为什么文档强调"先装 CANN toolkit，再装 TorchNPU"？
答案：因为 TorchNPU 是 PyTorch 与昇腾硬件的适配层，安装时需要对接已就位的 CANN 软件栈；顺序颠倒会导致适配失败。同时三者（PyTorch、TorchNPU、PyPTO）必须使用一致的 Python 版本。

### 4.2 源码编译与安装：build_ci.py 与 run 包

#### 4.2.1 概念说明

从这一节起，你手上有了仓库源码，要把它变成可安装的软件包。PyPTO 的构建入口是根目录下的 `build_ci.py`：一个用 Python 写的构建控制器，负责调用 CMake 编译 C++ 框架（`framework/`）、打包 Python 包（`python/`），最终产出一个 `.run` 安装包。

需要注意一个重要事实：**如果你使用 CANN 9.1.0 及之后的版本，PyPTO 已经集成在 CANN 包内，无需自己编译**；只有体验 master 分支新能力或参与框架开发时，才走本节的源码编译流程。

#### 4.2.2 核心流程

```text
git clone 源码
    ↓
python3 build_ci.py --clean --no_isolation     # 编译，产物输出到 build_out/
    ↓
build_out/cann-pypto_${版本}_${os_arch}.run    # 得到 run 安装包
    ↓
bash ./cann-pypto_*.run --full -q --pylocal    # 安装
    ↓
跑一个样例验证安装                              # 详见 4.3 / 4.4
```

#### 4.2.3 源码精读

**版本配套说明**

> [docs/zh/install/build_and_install.md:3-13](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/install/build_and_install.md#L3-L13) —— 官方给出 CANN 与 PyPTO 的版本对应表（如 CANN 8.5.0 ↔ PyPTO 0.1.2、CANN 9.0.0 ↔ PyPTO 0.2.0），并说明 CANN 9.1.0 及之后版本已内置 PyPTO，只有用 master 源码开发时才需要手动编译安装。

**下载与编译**

> [docs/zh/install/build_and_install.md:15-22](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/install/build_and_install.md#L15-L22) —— 用 `git clone -b ${tag_version}` 拉取与本地 CANN 版本配套的分支源码。

> [docs/zh/install/build_and_install.md:33-49](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/install/build_and_install.md#L33-L49) —— 在源码根目录执行 `python3 build_ci.py --clean --no_isolation` 编译 run 包；参数表中 `--clean` 表示编译前清理构建与安装输出目录，`--no_isolation` 表示关闭 whl 隔离构建模式（构建依赖需提前装好，正好对应 4.1 节先装 requirements.txt 的步骤）。

`build_ci.py` 自身的文件头注释 confirms 了它是"CI 场景构建控制总入口"，并列出了它支持的完整参数集：

> [build_ci.py:11-47](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/build_ci.py#L11-L47) —— 构建入口的说明文档：支持 whl 常规/可编辑模式编译、UTest/STest/Examples 执行、超时控制；常用选项包括 `-f` 前端类型、`-b` 后端类型、`-j` 并行度、`-c` 清理等。安装文档用到的 `--clean` 只是其中之一。

**安装 run 包**

> [docs/zh/install/build_and_install.md:51-66](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/install/build_and_install.md#L51-L66) —— 编译产物出现在 `build_out/` 目录，形如 `cann-pypto_9.1.0_linux-aarch64.run`；进入该目录执行 `bash ./cann-pypto_*.run --full -q --pylocal` 完成安装，`--full` 完整安装、`-q` 静默、`--pylocal` 把 python 相关内容装进 CANN 安装路径。

> [docs/zh/install/build_and_install.md:68-70](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/install/build_and_install.md#L68-L70) —— 安装验证的方式就是跑样例，即下一节的 hello_world。

#### 4.2.4 代码实践

1. **实践目标**：熟悉构建入口 `build_ci.py` 的能力面，为将来参与框架开发做准备（源码阅读型实践）。
2. **操作步骤**：
   - 通读 [build_ci.py:11-47](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/build_ci.py#L11-L47) 的模块注释。
   - 制作一张"参数 → 作用"表，至少包含 `-f`、`-b`、`-t`、`-j`、`--build_type`、`-u`、`-s`、`-c`、`--no_isolation`。
   - 有条件时执行 `python3 build_ci.py --help` 对照补充（该命令依赖 `packaging` 包已安装）。
3. **需要观察的现象**：`--help` 输出的参数分组与注释中描述是否一致。
4. **预期结果**：得到一张 9 行左右的参数对照表。`--help` 与真实编译的输出**待本地验证**（编译耗时较长且依赖完整环境）。

#### 4.2.5 小练习与答案

**练习 1**：你的机器装的是 CANN 9.2.0，想直接用 PyPTO，需要从源码编译吗？
答案：不需要。CANN 9.1.0 及之后的版本已集成 PyPTO，安装完 CANN 即可使用；源码编译只在体验 master 新特性或做框架开发时需要。

**练习 2**：`--no_isolation` 关闭了什么？为什么要先装 `python/requirements.txt`？
答案：它关闭 whl 的隔离构建模式。隔离模式下构建工具会临时拉起独立环境；关闭后构建依赖必须提前在当前环境装好，所以要先执行 `pip install -r python/requirements.txt`。

**练习 3**：编译完成后，安装包在哪个目录、长什么样？
答案：在源码根目录的 `build_out/` 下，文件名形如 `cann-pypto_${版本号}_${os_arch}.run`，用 `bash ./xxx.run --full -q --pylocal` 安装。

### 4.3 逐行精读 hello_world：第一个 PyPTO 算子

#### 4.3.1 概念说明

`hello_world.py` 是官方入门示例，只有 80 余行，却浓缩了 PyPTO 编程的三个核心语法：

1. **`@pypto.jit(runtime_options=...)` 装饰器**：把一个普通 Python 函数标记为"待编译的算子内核"。被装饰后，第一次用真实参数调用它时，框架才会捕获函数体、走编译链路（上一讲讲的 Tensor Graph → ... → CodeGen）、生成并缓存内核，然后执行。`runtime_options` 是传给编译/运行时的选项字典。
2. **`Tensor[...]` 类型标注**：写在函数签名上，意思是"这里传入一个 pypto Tensor，shape 和 dtype 不写死，由调用时的实参自动推断"。这就是官方注释里说的 auto infer。
3. **`out[:] = x + y` 写回语法**：PyPTO 内核不支持返回值，计算结果必须显式写入输出张量。`out[:] = ...` 是一个语法糖，等价写法是 `pypto.assemble(x + y, [0, 0], out)`（示例代码里的注释原话）。

另外还有一个"Tile"的身影：`pypto.set_vec_tile_shapes(32, 32)`。上一讲讲过，Tile 是恰好能放进核内私有缓存的数据块；这里为向量运算声明了 32×32 的 tile 形状。

#### 4.3.2 核心流程

`hello_world.py` 的执行流程：

```text
python3 hello_world.py --run_mode=sim
    ↓
main() 解析命令行参数（-m/--run_mode，默认 npu）
    ↓
device_init(run_mode)
    ├─ sim → runtime_options["run_mode"] = RunMode.SIM，返回 "cpu"
    └─ npu → 检查 torch_npu → 读 TILE_FWK_DEVICE_ID → 返回 "npu:{id}"
    ↓
用 torch 在对应 device 上创建 x、y、out（64×64 float）
    ↓
add_kernel(x, y, out)   ← 首次调用触发 JIT 编译，然后执行
    ↓
torch.testing.assert_close(x + y, out) 校验结果
```

顺带算一笔 tile 账：输入 shape 是 \(64 \times 64\)，tile 是 \(32 \times 32\)，每个维度需要 \(\lceil 64/32 \rceil = 2\) 块，整张张量共 \(2 \times 2 = 4\) 个 tile 块。框架会自动按 tile 切分搬运与计算——这正是上一讲"声明式编程 + 框架自动 tiling"的具体体现。

#### 4.3.3 源码精读

**内核定义（本讲最重要的一段代码）**

> [examples/00_hello_world/hello_world.py:23-36](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/00_hello_world/hello_world.py#L23-L36) —— 这 14 行就是完整的 PyPTO 算子：
> - L23 定义 `runtime_options = {"run_mode": pypto.RunMode.NPU}`，一个可变字典；
> - L26 `@pypto.jit(runtime_options=runtime_options)` 装饰函数；
> - L27 三个参数都标注 `pypto.Tensor[...]`，shape/dtype 调用时自动推断；
> - L32 `pypto.set_vec_tile_shapes(32, 32)` 设置向量 tile 形状，注释强调其 rank 必须与 `x`、`y` 匹配；
> - L36 `out[:] = x + y` 把结果写入输出张量；注释说明内核不支持返回值，`[:]` 只是写回的语法糖，也可用 `pypto.assemble(x + y, [0, 0], out)`。

**模式切换与设备初始化**

> [examples/00_hello_world/hello_world.py:39-54](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/00_hello_world/hello_world.py#L39-L54) —— `device_init` 按运行模式分两条路：`sim` 时把 `runtime_options["run_mode"]` 改成 `RunMode.SIM` 并返回 `"cpu"`（不 import torch_npu）；`npu` 时先检查 `torch_npu` 是否安装（缺则报错退出），再从环境变量 `TILE_FWK_DEVICE_ID` 读设备号（默认 0）、`torch.npu.set_device` 绑定设备，最后返回 `"npu:{device_id}"`。注意它修改的是模块级 `runtime_options` 字典的内容——装饰器持有的是同一个字典的引用，而内核在 `main` 里才被首次调用，所以这里改模式来得及生效。

**主流程**

> [examples/00_hello_world/hello_world.py:57-78](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/00_hello_world/hello_world.py#L57-L78) —— `main` 用 argparse 提供 `-m/--run_mode`（可选 `npu`/`sim`，默认 `npu`）；L68 定死输入 shape 为 `(64, 64)`；L71-73 用 torch 生成随机输入 `x`、`y` 和空输出 `out`；L75 `add_kernel(x, y, out)` 首次调用触发编译并执行；L77 用 `torch.testing.assert_close(x + y, out, atol=1e-3, rtol=1e-3)` 与 PyTorch 的 CPU 参考实现比对，容差 1e-3。

**官方运行命令与产物**

> [docs/zh/invocation/examples_invocation.md:3-18](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/invocation/examples_invocation.md#L3-L18) —— 无真机时：`source .../set_env.sh` 后进入示例目录执行 `python3 hello_world.py --run_mode=sim`；有真机时：额外 `export TILE_FWK_DEVICE_ID=0`，用 `--run_mode=npu` 运行。

> [docs/zh/invocation/examples_invocation.md:22-24](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/invocation/examples_invocation.md#L22-L24) —— 运行成功后会在 `${work_path}/output/` 生成编译与运行产物，包括**计算图**和**泳道图**，可用 PyPTO Toolkit 插件在 VS Code 中查看并与源码关联。

**一份等价的"快速开始"写法**

> [docs/zh/invocation/examples_invocation.md:30-71](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/invocation/examples_invocation.md#L30-L71) —— 文档里的另一种等价写法：在函数内用 `pypto.frontend.jit` 现场装饰（`pypto.jit` 就是它的别名，见 [python/pypto/__init__.py:48](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/__init__.py#L48) 的 `jit = frontend.jit`），并用 `pypto.Tensor([...], pypto.DT_FP32)` 显式指定了 dtype。对照示例可以看出 `Tensor[...]` 与 `Tensor([...], DT_FP32)` 两种标注风格：前者全推断，后者显式声明数据类型。

#### 4.3.4 代码实践

1. **实践目标**：跑通第一个算子，并亲眼看到编译产物。
2. **操作步骤**：
   - `source` CANN 环境变量（路径以实际安装为准）；
   - `cd examples/00_hello_world && python3 hello_world.py --run_mode=sim`（有真机则用 `--run_mode=npu`，并先 `export TILE_FWK_DEVICE_ID=0`）；
   - 先执行 `python3 hello_world.py --help`，确认 `-m/--run_mode` 参数存在；
   - 运行结束后，在执行目录附近查找 `output/` 目录，看看有没有计算图 / 泳道图产物文件。
3. **需要观察的现象**：终端最终打印 `✓ Test add_kernel completed successfully`；`output/` 目录中出现的文件名与格式。
4. **预期结果**：assert_close 通过（说明 PyPTO 算子结果与 torch 参考实现一致）；产物目录生成计算图与泳道图文件（若安装了 Toolkit 插件可可视化查看）。本实践依赖已装好的环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把 `add_kernel` 里的 `out[:] = x + y` 改成 `return x + y` 会怎样？
答案：不行。源码注释明确说明 pypto 内核不支持返回值（[examples/00_hello_world/hello_world.py:34-36](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/00_hello_world/hello_world.py#L34-L36)），结果必须写入输出张量，`out[:]` 只是语法糖，等价 API 是 `pypto.assemble(x + y, [0, 0], out)`。

**练习 2**：输入 shape 为 `(64, 64)`、tile 为 `(32, 32)` 时，数据会被切成几块？如果把 tile 改成 `(16, 32)` 呢？
答案：\(2 \times 2 = 4\) 块；改成 `(16, 32)` 后为 \(\lceil 64/16 \rceil \times \lceil 64/32 \rceil = 4 \times 2 = 8\) 块。tile 变小意味着搬运次数变多，这是后面性能调优的基础直觉。

**练习 3**：`assert_close` 里的 `atol=1e-3, rtol=1e-3` 起什么作用？
答案：允许绝对误差和相对误差在千分之一以内。浮点运算在 NPU 上的累加顺序可能与 CPU 不同，逐位相等通常做不到，所以数值校验要带容差。

### 4.4 两种运行模式：RunMode.NPU 与 RunMode.SIM

#### 4.4.1 概念说明

`RunMode` 决定编译出的内核在哪里执行：

- **`RunMode.NPU`（真机模式）**：编译产物下发到真实 NPU 的 AI Core 上执行，结果从设备回读。需要驱动、CANN ops、torch_npu 全套运行态依赖。
- **`RunMode.SIM`（仿真模式）**：在主机侧用仿真/解释的方式执行编译产物，不需要真实 NPU。对学习者极其重要——没有昇腾卡也能把整个编译链路走完、验证数值正确性，代价是拿不到真实性能数据。

`RunMode` 是一个真实的枚举类型，定义在运行时模块里（不是文档杜撰的概念）。

#### 4.4.2 核心流程

```text
命令行 --run_mode
    ├─ "sim" → device_init 返回 "cpu"，数据放 CPU，内核走仿真执行
    └─ "npu" → device_init 返回 "npu:{id}"，数据放 NPU，内核走真机执行
两种模式共用同一个 add_kernel 源码 —— 只是 runtime_options["run_mode"] 的值不同
```

关键点：**算子代码本身对运行模式无感知**。模式只影响 `runtime_options` 和数据所在设备，这让你可以在 SIM 模式下开发调试、再无缝切到 NPU 模式跑性能。

#### 4.4.3 源码精读

**RunMode 的真实定义**

> [python/pypto/runtime.py:38-41](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/runtime.py#L38-L41) —— `RunMode(IntEnum)` 只有两个成员：`NPU = 0`、`SIM = 1`。它经由包入口导出给用户。

> [python/pypto/__init__.py:38-48](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/__init__.py#L38-L48) —— 包入口把 `RunMode` 从 `.runtime` 导出（L38），`Tensor` 从 `.tensor` 导出（L40），而 `pypto.jit` 其实是 `frontend.jit` 的别名（L48）。所以示例里 `pypto.RunMode.SIM`、`pypto.jit` 这些写法都有真实来源。

**hello_world 中的模式切换**

> [examples/00_hello_world/hello_world.py:39-54](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/00_hello_world/hello_world.py#L39-L54) —— 再次聚焦这段代码：sim 分支（L40-42）只改字典、返回 `"cpu"`，全程不需要 `torch_npu`；npu 分支（L44-54）才会 `import torch_npu`、读 `TILE_FWK_DEVICE_ID` 环境变量并 `torch.npu.set_device(device_id)`。`TILE_FWK_DEVICE_ID` 就是多卡环境下选择用哪张卡的官方开关。

**文档中的对应命令**

> [docs/zh/invocation/examples_invocation.md:3-18](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/invocation/examples_invocation.md#L3-L18) —— 仿真环境三行命令 vs 真实环境四行命令（多一行 `export TILE_FWK_DEVICE_ID=0`），这是本讲最值得背下来的两条命令。

> [docs/zh/invocation/examples_invocation.md:73-74](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/docs/zh/invocation/examples_invocation.md#L73-L74) —— 文档补充了两种模式查看结果的方式差异：真实环境或精度仿真直接看输出张量的值；性能仿真通过 `output/` 下的泳道图查看。

#### 4.4.4 代码实践

1. **实践目标**：亲手验证两种模式的差异，并理解 `device_init` 的分支逻辑。
2. **操作步骤**：
   - 复制示例为 `my_hello.py`（保留原文件）：`cp examples/00_hello_world/hello_world.py examples/00_hello_world/my_hello.py`；
   - 在 `my_hello.py` 的 `device_init` 两个分支里各加一行 `print(f"[trace] run_mode branch: ...")`（改的是你自己的副本，不动仓库源码）；
   - 有真机：分别执行 `--run_mode=sim` 和 `--run_mode=npu`；无真机：只跑 `sim`，npu 分支用源码阅读完成；
   - 附加观察：npu 模式下试试 `export TILE_FWK_DEVICE_ID=1`（如果有多卡），看 trace 里设备号变化。
3. **需要观察的现象**：sim 模式下 trace 显示返回 `"cpu"`、没有触发 `import torch_npu`；npu 模式下 trace 显示 `"npu:0"`（或你指定的卡号）。
4. **预期结果**：两种模式都打印 `✓ Test add_kernel completed successfully`，结果一致。无真机时 npu 分支行为**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：SIM 模式下，`x`、`y`、`out` 这三个张量实际放在什么设备上？需要安装 torch_npu 吗？
答案：放在 CPU 上（`device_init` 的 sim 分支返回 `"cpu"`，见 [examples/00_hello_world/hello_world.py:40-42](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/examples/00_hello_world/hello_world.py#L40-L42)）；不需要，`import torch_npu` 只出现在 npu 分支里。

**练习 2**：`TILE_FWK_DEVICE_ID` 环境变量的作用是什么？
答案：指定真机模式下使用哪张 NPU 卡，示例中用它初始化 `torch.npu.set_device(device_id)` 并构造 `"npu:{device_id}"` 设备字符串；不设置时默认为 0。

**练习 3**：`RunMode` 枚举有几个成员、值分别是多少？
答案：两个：`NPU = 0`、`SIM = 1`，定义在 [python/pypto/runtime.py:38-41](https://github.com/gitcode.com/cann/pypto/blob/0fc1037031e6f5b3fb0e9f68d49b4fdfd542c32e/python/pypto/runtime.py#L38-L41)，是 `IntEnum`。

## 5. 综合实践

**任务**：搭建环境后运行 hello_world，并把向量加法改造成 `x + 2 * y`，验证输出正确。（本综合实践对应大纲规定的实践任务，全部步骤依赖已装好的环境，**待本地验证**。）

**第 1 步：环境就绪检查**

- 按 4.1 节完成任一方式的环境搭建与 `pip install -r python/requirements.txt`。
- 按 4.2 节安装 PyPTO（CANN ≥ 9.1.0 用户可跳过源码编译）。

**第 2 步：跑通原版**

```bash
cd examples/00_hello_world
python3 hello_world.py --run_mode=sim        # 无真机
# 有真机：export TILE_FWK_DEVICE_ID=0 && python3 hello_world.py --run_mode=npu
```

预期看到 `✓ Test add_kernel completed successfully`。

**第 3 步：复制并修改内核**（改副本，保留原文件）

```python
# 示例代码：修改自 examples/00_hello_world/hello_world.py，仅改动内核一行
@pypto.jit(runtime_options=runtime_options)
def add_kernel(x: pypto.Tensor[...], y: pypto.Tensor[...], out: pypto.Tensor[...]):
    pypto.set_vec_tile_shapes(32, 32)
    out[:] = x + 2 * y        # 原来是 out[:] = x + y
```

同时把校验行同步改成参考实现：

```python
# 示例代码：校验也要换成 x + 2 * y，否则必然断言失败
torch.testing.assert_close(x + 2 * y, out, atol=1e-3, rtol=1e-3)
```

**第 4 步：运行并验证**

1. 再次以 sim（或 npu）模式运行修改后的文件。
2. 观察是否打印成功信息；如果失败，读报错定位（常见坑：只改了内核没改校验行）。
3. 可选加强：在调用内核前手动 `print((x + 2 * y)[0, :4])`、调用后 `print(out[0, :4])`，肉眼比对前 4 个元素。

**第 5 步：观察产物**

查看 `${work_path}/output/` 目录：计算图应随内核表达式的变化而变化（多了一个乘法节点）；如装有 PyPTO Toolkit 插件，可在 VS Code 里打开泳道图与代码关联查看。

**预期结果**：assert_close 通过，说明你写下的第一个自定义表达式 `x + 2 * y` 被 PyPTO 完整走完了"捕获 → 编译 → 执行 → 数值校验"全链路。

## 6. 本讲小结

- PyPTO 环境分三层：驱动固件 + CANN（toolkit/ops）+ Python/编译工具；无真机可选 CANNLab 或 SIM 模式，有真机求快选 Docker、求新选手动安装。
- CANN 9.1.0 及之后版本已内置 PyPTO；源码编译用 `python3 build_ci.py --clean --no_isolation` 产出 `build_out/` 下的 run 包，再用 `--full -q --pylocal` 安装。
- PyPTO 算子三要素：`@pypto.jit` 装饰器声明内核、`Tensor[...]` 标注实现 shape/dtype 自动推断、`out[:] = ...` 写回输出（内核不支持返回值）。
- `set_vec_tile_shapes(32, 32)` 声明向量运算的 tile 形状，其 rank 必须与输入张量匹配；\(64 \times 64\) 的输入配 \(32 \times 32\) 的 tile 共 4 块。
- `RunMode.NPU`（真机，需 torch_npu，可用 `TILE_FWK_DEVICE_ID` 选卡）与 `RunMode.SIM`（仿真，CPU 数据，无需真机）共用同一份算子代码，是贯穿后续所有讲义的开发调试方式。
- 运行成功后 `${work_path}/output/` 会生成计算图与泳道图产物，可用 Toolkit 插件在 VS Code 中查看。

## 7. 下一步学习建议

- 下一讲（u1-l3 仓库目录结构与源码地图）将带你把本讲提到的 `python/pypto`、`build_ci.py`、`framework/` 等位置串成完整的源码地图，建议先在本讲基础上浏览一遍仓库根目录。
- 想先多写几个算子找手感，可以直接跳去 u2-l1（Tensor 对象与张量创建）和 u2-l2（算子 API 体系），再回来补源码地图。
- 延伸阅读：`docs/zh/tutorials/introduction/quick_start.md`（快速开始，含计算图/泳道图查看方法）；`examples/README.md`（示例总览，u1-l4 的主素材）。
