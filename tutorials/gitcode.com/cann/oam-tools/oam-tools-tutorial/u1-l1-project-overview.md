# oam-tools 是什么：项目定位与四大组件

## 1. 本讲目标

学完本讲，你应该能够：

- 用一句话说清 OAM-Tools 是什么、解决什么问题。
- 说出 asys、msaicerr、msprof、hccl_test 四大组件各自的功能定位和典型命令。
- 知道工具集支持哪些昇腾 AI 处理器和 CPU 架构，以及如何用 `npu-smi info` 判断自己的设备是否在支持范围内。
- 找到每个组件的仓内文档入口（`docs/zh/` 下对应目录）和官方线上用户指南。

本讲是整本学习手册的第一讲，不要求你写代码，只要求你"把地图看懂"。后续所有讲义都会在这张地图上展开。

## 2. 前置知识

本讲需要的背景概念很少，遇到以下术语时按下面的通俗解释理解即可：

- **CANN**：华为昇腾 AI 处理器的异构计算架构，可以类比"NVIDIA 那边的 CUDA"。它包含驱动、运行时、算子库和一套工具链。CANN 安装后有一个 `set_env.sh` 脚本，`source` 它就能加载全部环境变量。
- **OAM**：Operations, Administration and Maintenance（运行、管理、维护），电信和网络领域的一个经典说法，指"设备跑起来之后，运维人员用来管它、查它故障的那套工具"。
- **昇腾 AI 处理器（NPU）**：华为的 AI 加速芯片，例如 910B、910_93、950 等。`npu-smi info` 是查看 NPU 状态的基础命令（类比 `nvidia-smi`）。
- **AI Core Error**：AI Core 是 NPU 上执行算子计算的核。当算子计算出错（如越界、溢出）、核挂死时上报的错误就是 AI Core Error，是昇腾故障定位中最常见的一类问题。
- **HCCL**：Huawei Collective Communication Library，华为集合通信库，分布式训练时多卡之间 allreduce、allgather 等通信的底层库（类比 NCCL）。
- **Dump 文件**：把算子输入输出张量落到磁盘上的二进制文件，常用于精度问题分析。
- **`.run` 包**：昇腾软件常用的自解压安装包格式，加 `--full` 参数执行即可安装。

不需要真的有昇腾设备也能读懂本讲；没有设备时可以通过 docker 编译构建（详见 `docs/zh/quick_install.md`）。

## 3. 本讲源码地图

本讲涉及的"源码"以项目说明文档为主，它们是理解整个仓库的入口：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目主 README：项目定位、四大组件功能表、目录结构、编译安装与验证、支持硬件列表、文档索引 |
| `docs/zh/asys/README.md` | asys 组件的中文文档目录页，列出 19 篇子文档（功能约束、采集、解析、FAQ 等） |
| `docs/zh/msaicerr/README.md` | msaicerr 组件的中文文档目录页（功能约束、AI Core Error 分析、Dump 解析、环境检查） |
| `docs/zh/profiling/README.md` | msprof 性能数据采集中文文档目录页（msprof 命令、其他采集方式、附录） |
| `docs/zh/hccl_test/README.md` | hccl_test 中文文档目录页（介绍、安装编译、执行、参数、约束、FAQ） |

后续讲义会深入 `src/` 下的真实代码，本讲先把这五个文件的"地图信息"读透。

## 4. 核心概念与源码讲解

### 4.1 README 项目导读

#### 4.1.1 概念说明

打开任何一个开源项目，第一件事都是读 README。oam-tools 的主 README 信息密度很高，一段话讲清了项目定位：

> OAM-Tools（Operations, Administration, and Maintenance）是华为 CANN 的开源运维工具集，为昇腾 AI 处理器开发者提供**故障定位**与**性能调优**两大核心能力。

拆开理解：

1. **它是"工具集"而不是单一工具**——仓库里装了四个相互独立的工具（下一节展开）。
2. **它服务的是"昇腾 + CANN"生态**——工具运行依赖 CANN 环境（`set_env.sh`），安装后释放到 CANN 安装目录的 `tools/` 子目录下，而不是装到系统任意位置。
3. **两大能力主线是故障定位和性能调优**——asys/msaicerr 属于前者，msprof 属于后者，hccl_test 专注通信测试。

README 还给出了三个典型适用场景，这三种场景正好对应四件工具的分工：

- 训练/推理异常时一键采集故障信息、分析 AI Core Error 根因 → **asys + msaicerr**
- 性能调优，采集各阶段性能指标定位瓶颈 → **msprof**
- 分布式场景下测试集合通信功能与性能 → **hccl_test**

#### 4.1.2 核心流程

从拿到仓库到用上工具的整体流程（本讲只需建立印象，细节在 u1-l2 ~ u1-l4 展开）：

```text
克隆仓库
   │
   ▼
source <CANN安装路径>/set_env.sh        # 加载 CANN 环境
   │
   ▼
bash build.sh                          # CMake 编译 + 下载三方库/闭源 bundle + 打 .run 包
   │
   ▼
./build_out/cann-oam-tools_<版本>_linux-<架构>.run --full   # 安装
   │
   ▼
工具释放到 ${ASCEND_INSTALL_PATH}/tools/ 下各子目录
   │
   ▼
asys / msaicerr / msprof 分析脚本 / hccl_test 可执行文件投入使用
```

一个值得注意的架构事实（README「项目架构」一节）：四大组件**相互独立又协同工作**，全部通过统一的构建系统（CMake + build.sh）编译打包。这意味着你可以只学自己关心的那个组件，而不必先理解全部四个。

#### 4.1.3 源码精读

**项目定位与适用场景**，见 README 项目简介一节：

[README.md:12-19](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L12-L19)

这段定义了 OAM-Tools 的身份（CANN 开源运维工具集）、两大核心能力（故障定位、性能调优）和三个适用场景（异常采集分析、性能调优、通信测试）。

**目录结构总览**，见 README 中的目录树：

[README.md:36-58](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L36-L58)

注意几个关键目录的含义：

- `src/asys/`：asys 工具，**纯 Python** 实现。
- `src/msaicerr/`：msaicerr 工具，**Python** 为主（内部会调用 C++ 编译出的 proto 解析工具，u3-l3 详讲）。
- `src/msprof/`：msprof 工具，**C++ collector + Python 分析脚本**的混合形态。
- `src/hccl_test/`：hccl_test 工具，**C++** 实现。
- `test/`：UT/ST 测试用例；`docs/`：中英文文档；`build.sh` / `CMakeLists.txt` / `version.cmake`：构建三件套。

**编译 → 安装 → 验证的最短路径**，见快速开始与源码编译两节：

[README.md:60-84](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L60-L84)

三步：`source set_env.sh` → `bash build.sh` → 执行 `build_out/` 下生成的 `.run` 包安装。产物命名规则 `cann-oam-tools_<cann_version>_linux-<arch>.run` 中的 `<arch>` 取值为 `x86_64` 或 `aarch64`。

**安装后工具的落位与调用方式**，见功能运行示例一节：

[README.md:183-252](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L183-L252)

这一段是本讲最重要的"使用地图"：

- asys 安装后在 `tools/ascend_system_advisor/asys/`，且因为 `src/asys/asys` 是指向 `asys.py` 的软链接、带 shebang，所以 `asys -h` 和 `python3 .../asys.py -h` 两种调用都可用（[README.md:199-222](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L199-L222)）。
- msaicerr 安装后在 `tools/msaicerr/msaicerr.py`（[README.md:224-240](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L224-L240)）。
- msprof 的分析 wheel 自动解包到 `tools/profiler/profiler_tool/`，不需要手动 `pip install`（[README.md:242-252](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L242-L252)）。

**官方用户指南索引**，见相关文档一节：

[README.md:259-269](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L259-L269)

四个组件各有一条 hiascend.com 官方文档跳转链接，是比仓内文档更完整的用户手册。

#### 4.1.4 代码实践

**实践目标**：不写代码，通过"读 + 查"建立对仓库的第一手认知，验证 README 里的事实。

**操作步骤**：

1. 在仓库根目录打开 `README.md`，通读一遍（约 280 行）。
2. 用下面的命令对照 README 的目录树，确认每个目录真实存在：

   ```bash
   ls -d src/asys src/msaicerr src/msprof src/hccl_test test docs/zh build.sh CMakeLists.txt version.cmake
   ```

3. 确认 asys 软链接的存在（README 声称 `src/asys/asys -> ./asys.py`）：

   ```bash
   ls -l src/asys/asys
   head -1 src/asys/asys.py    # 应看到 #!/usr/bin/env python3
   ```

4. 查看 `docs/zh/` 下有哪些组件文档目录：

   ```bash
   ls docs/zh
   ```

**需要观察的现象**：

- 第 2 步所有路径都应无报错地列出。
- 第 3 步 `ls -l` 输出中 `src/asys/asys -> ./asys.py`，且 `head` 输出 shebang 行。
- 第 4 步能看到 `asys`、`msaicerr`、`profiling`、`hccl_test` 等目录（注意性能文档目录名是 `profiling` 而不是 `msprof`）。

**预期结果**：README 描述的目录结构与磁盘实际内容一致。若某一步不符（例如在旧版本代码上），说明 README 与代码版本有出入，应以源码为准。

（以上命令在任何 Linux 环境即可执行，不需要昇腾设备。）

#### 4.1.5 小练习与答案

**练习 1**：OAM-Tools 的两大核心能力是什么？分别对应哪些组件？

**参考答案**：故障定位与性能调优两大核心能力。故障定位由 asys（故障信息采集与诊断）和 msaicerr（AI Core Error 分析）承担；性能调优由 msprof 承担；hccl_test 专注分布式集合通信的功能与性能测试，是相对独立的一支。

**练习 2**：安装 `.run` 包后，asys 工具会被释放到哪个目录？为什么可以直接敲 `asys -h` 而不用写 `python3`？

**参考答案**：释放到 `${ASCEND_INSTALL_PATH}/tools/ascend_system_advisor/asys/`（root 用户默认为 `/usr/local/Ascend/cann/tools/ascend_system_advisor/asys/`）。因为仓库里 `src/asys/asys` 是指向 `asys.py` 的软链接，`asys.py` 首行有 `#!/usr/bin/env python3` shebang，且 CMake 用 `install(DIRECTORY ...)` 原样拷贝保留了软链接，所以两种调用方式都有效。

**练习 3**：如果你只想跑 asys 的测试，应该执行什么命令？

**参考答案**：`bash build.sh -u --component asys`。`-u` 表示运行测试，`--component` 指定组件，asys 覆盖其 Python UT + ST。

### 4.2 四大组件功能表

#### 4.2.1 概念说明

README 用一张"组件 / 功能定位 / 核心能力"三列表格概括四大组件，这是全仓库最浓缩的一张图。理解它的关键是抓住每个组件的"一句话人设"：

| 组件 | 一句话人设 | 语言/形态 |
| --- | --- | --- |
| **asys** | 昆虫界的"一键采集器"：出了故障，一条命令把现场全部收走 | 纯 Python |
| **msaicerr** | AI Core Error 专科医生：拿到错误报告和 Dump 文件，给出诊断 | Python（内部调 C++） |
| **msprof** | 性能摄影师：给 AI 任务各阶段"拍照"，再分析慢在哪 | C++ collector + Python 分析 |
| **hccl_test** | 通信质检员：逐个测集合通信算子的正确性和带宽 | C++ |

两个容易混淆的点提前澄清：

- **asys 和 msaicerr 都能碰 AI Core Error，分工是什么？** asys 负责"收集现场"（launch/collect 时把 AI Core Error 相关故障信息采集打包），msaicerr 负责"深度解析"（解析报告目录、解析 Dump 文件、检查运行环境）。u2-l7 会讲 asys analyze 与 msaicerr 的配合关系。
- **msprof 里 "collector" 和"分析脚本"是什么关系？** C++ 侧 collector（`basic`、`dvvp` 两个目录）负责在业务运行时采集原始性能数据；Python 侧的 `msprof` wheel 负责把原始数据分析成可读结果。采集与分析是分离的。

#### 4.2.2 核心流程

四个组件在运维链路上的位置可以这样看：

```text
                 ┌────────────── 故障定位线 ──────────────┐
业务运行异常 ──▶ asys（采集现场：info/health/collect/launch）
                    │  产出故障信息包
                    ▼
                 msaicerr（解析 AI Core Error 报告 / Dump 文件 / 环境检查）
                    │  给出错误码与根因线索
                    ▼
                 开发者定位修复

                 ┌────────────── 性能/通信线 ─────────────┐
业务跑得慢   ──▶ msprof（采集各阶段性能数据 ──▶ 分析瓶颈）
多卡通信疑虑 ─▶ hccl_test（allreduce 等算子的正确性 + 带宽测试）
```

每个组件的典型命令（来自 README，可直接对照记忆）：

- asys：`asys info -r="status" -d=0`、`asys health`、`asys collect --output <dir>`
- msaicerr：`python3 msaicerr.py -p <report_dir> -out <dir> -dev 0`（解析报告）、`-d <dump_file> -dtype float16`（解析 Dump）、`-e -dev 0`（环境检查）
- msprof：分析脚本入口 `profiler_tool/analysis/msprof/msprof.py`，collector 由 CANN profiler 流水线内部调用
- hccl_test：编译出可执行文件后按 hostfile 启动（详见 `docs/zh/hccl_test/execution.md`）

#### 4.2.3 源码精读

**四大组件功能总表**（本讲的核心表格）：

[README.md:21-30](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L21-L30)

这张表列出：asys 的十来项核心能力（采集、复跑、状态展示、健康检查、综合/组件检测、trace/coredump/stackcore/coretrace/UB 文件解析、实时堆栈导出、AI Core Error 解析、性能数据采集）；msaicerr 的三项（AI Core Error 分析、Dump 解析与类型转换、环境检查）；msprof 的数据类别与多种采集方式；hccl_test 的定位。后续单元的讲义标题几乎都能在这张表里找到原型。

**asys 的子命令集合**，README 明确指出定义位置：

[README.md:211-222](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L211-L222)

asys 共 8 个子命令：`info / health / collect / launch / diagnose / analyze / config / profiling`，定义在 `src/asys/cmdline/cmd_parser.py` 的 `Command` 枚举中。u1-l3 会带你在源码里找到它。

**msaicerr 的三种工作模式**：

[README.md:224-240](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L224-L240)

`-p`（解析 AI Core Error 报告目录）、`-d`（解析单个 Dump 文件）、`-e`（环境检查）三选一，这是 msaicerr 入口分发的全部模式（u3-l1 逐行讲）。

**msprof 的组成与安装形态**：

[README.md:242-252](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L242-L252)

关键事实：collector 分 `basic` 与 `dvvp` 两部分；分析 wheel（`msprof-0.0.1-py3-none-any.whl`）打包进 `.run` 并自动解包到 `tools/profiler/profiler_tool/`，不在 `PATH` 注册命令；C++ UT 回归命令为 `bash build.sh -u --component msprof`。

**各组件仓内文档入口**（本讲的"文档地图"）：

- asys 文档目录页（19 篇子文档，覆盖全部子命令与文件解析）：[docs/zh/asys/README.md:1-21](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/asys/README.md#L1-L21)
- msaicerr 文档目录页（6 篇：功能约束、环境准备、AI Core Error 分析、Dump 解析、Dump 类型转换、环境检查）：[docs/zh/msaicerr/README.md:1-6](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/msaicerr/README.md#L1-L6)
- msprof（性能数据采集）文档目录页（msprof 命令、acl/Ascend Graph/acl.json/环境变量等采集方式、附录）：[docs/zh/profiling/README.md:1-33](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/README.md#L1-L33)
- hccl_test 文档目录页（介绍、安装编译、执行、参数、约束、FAQ 六篇）：[docs/zh/hccl_test/README.md:1-7](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/README.md#L1-L7)

一个阅读技巧：msprof 文档里的 `<!-- npu="950,A3,910b,..." -->` 注释（如 [docs/zh/profiling/README.md:9-15](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/README.md#L9-L15)）是按芯片型号控制文档条目是否展示的条件编译标记，说明部分采集能力只在特定芯片上提供——这不是文档错误，读文档时要留意。

#### 4.2.4 代码实践

**实践目标**：亲手整理出"四大组件功能定位对照表"，并从组件文档中为每个组件补一条典型使用场景，形成自己的速查卡。

**操作步骤**：

1. 画一张三列表格（组件名 / 解决什么问题 / 典型命令），先只凭 README 第 21-30 行的功能表和第 183-252 行的运行示例填写。参考骨架：

   | 组件 | 解决什么问题 | 典型命令 |
   | --- | --- | --- |
   | asys | （待填） | `asys health` |
   | msaicerr | （待填） | `python3 msaicerr.py -e -dev 0` |
   | msprof | （待填） | （待填） |
   | hccl_test | （待填） | （待填） |

2. 打开 `docs/zh/asys/README.md`，从 19 篇子文档标题里挑一篇与你的工作最相关的（例如 `fault_information_collection.md` 故障信息收集），点进去略读，把"这条命令在什么场景下用"写成一句话。
3. 对 `docs/zh/msaicerr/README.md`（推荐 `AI_Core_error_analysis.md`）、`docs/zh/profiling/README.md`（推荐 `msprof_cmd/msprof_cmd.md`）、`docs/zh/hccl_test/README.md`（推荐 `introduction.md`）重复第 2 步。
4. 把四条场景合并进你的表格，形成第四列"典型使用场景"。

**需要观察的现象**：每个组件文档的粒度差异——asys 文档最细（一个子命令一篇），msaicerr 一个功能一篇，hccl_test 是传统用户指南结构，profiling 文档带芯片条件标记。这个差异本身就反映了组件的复杂度和用户群不同。

**预期结果**：得到一张 4 行 4 列的速查表，后续每一单元开始前扫一眼即可恢复记忆。（本实践为文档阅读型，不需要运行环境；如果本地有昇腾设备，可额外把表中命令真实跑一遍 `-h` 验证参数存在。若无法运行，标注"待本地验证"即可。）

#### 4.2.5 小练习与答案

**练习 1**：asys 和 msaicerr 都涉及 AI Core Error，二者如何分工？

**参考答案**：asys 是"采集端"——在故障发生现场收集 AI Core Error 相关的故障信息并打包（collect/launch 子命令），也提供 `asys analyze` 做解析入口；msaicerr 是"解析端"——专门解析 AI Core Error 报告目录（`-p`）、解析 Dump 文件并转换数据类型（`-d`/`-dtype`）、检查运行环境（`-e`）。前者管"拿到现场"，后者管"读懂现场"。

**练习 2**：想解析一个 Dump 文件并按 float16 查看数据，应该用哪个组件、什么命令？

**参考答案**：用 msaicerr：`python3 ${ASCEND_INSTALL_PATH}/tools/msaicerr/msaicerr.py -d <dump_file> -out <output_dir> -dtype float16`。Dump 文件解析与数据类型转换是 msaicerr 的核心能力之一。

**练习 3**：msprof 为什么说"开发者无需直接执行 C++ 侧 collector"？

**参考答案**：因为 C++ collector（basic、dvvp）是作为 CANN profiler 流水线的内置组件被调用的；面向用户的入口是 `msprof` 命令、acl/Ascend Graph API、acl.json 配置或环境变量等采集方式，这些方式内部再驱动 collector 工作。分析脚本则位于安装目录 `tools/profiler/profiler_tool/analysis/msprof/msprof.py`。

### 4.3 支持硬件列表

#### 4.3.1 概念说明

运维工具直接和芯片打交道，"支持哪些硬件"是硬约束。README 用一张表把昇腾处理器支持范围讲清楚了，理解它需要先建立三个概念的对应关系：

- **`npu-smi info` 的 Name 列**：你在机器上执行 `npu-smi info` 时实际看到的芯片名字符串。
- **适用产品**：该芯片对应的商业产品系列名（Atlas A2、A3、950 等）。
- **CANN ops 包代号**：安装 CANN 时对应芯片的软件包代号。

三者的关系是"同一个芯片的三个名字"。特别要注意两处易错点（README 明确强调）：

1. **`910B` 是关键字匹配**：实际可能显示 `910B1` / `910B2` / `910B3` / `910B4` 等带子型号的字符串，按"Name 列包含关键字"匹配即可。
2. **"910C" 是商用别称**：业内说的 910C 对应表中的 `910_93`，其 CANN ops 包自 CANN 8.5.0 起统一命名为 `Ascend-cann-A3-ops_*`，包名里不要写 `910c`、`910_c`、`910_93`。

另外两个维度：CPU 架构支持 `aarch64` 和 `x86_64`；没有昇腾设备也可以用 docker 方式编译构建（链接见 README 支持硬件一节）。

#### 4.3.2 核心流程

判断"我的机器能不能用 oam-tools"的流程：

```text
执行 npu-smi info
   │
   ▼
看 Name 列是否包含 910B / 910_93 / 950 之一
   │
   ├─ 是  ──▶ 支持该芯片；按对应 CANN ops 包代号（910b / A3 / 950）准备 CANN 环境
   └─ 否  ──▶ 当前不支持；可提交 issue 反馈，或仅用 docker 编译学习源码
   │
   ▼
确认 CPU 架构为 aarch64 或 x86_64（决定 .run 包名中的 <arch>）
```

这个映射在源码中也有体现：asys 的芯片适配层为 `ascend910B`、`ascend910_93`、`ascend910_96`、`ascend950` 等分别建立了 handler 目录（u2-l4 详讲）；msaicerr 也按芯片型号分流解析逻辑（u3-l2 详讲）。也就是说，README 这张硬件表是后续"芯片适配层"讲义的种子。

#### 4.3.3 源码精读

**支持硬件一节（CPU 架构 + 处理器对照表 + 两条注意事项）**：

[README.md:86-101](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L86-L101)

表格逐行含义：

| `npu-smi info` Name 列 | 适用产品 | CANN ops 包代号 |
| --- | --- | --- |
| `910B`（含 910B1~910B4） | Atlas A2 训练系列 / Atlas 800I A2 推理 | `910b` |
| `910_93` | Atlas A3 训练 / 推理系列（业内"910C"） | `A3` |
| `950` | Atlas 950 系列 | `950` |

**编译产物命名与架构的对应**：

[README.md:133-133](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L133)

编译产物为 `cann-oam-tools_<cann_version>_linux-<arch>.run`，`<arch>` 取 `x86_64` 或 `aarch64`——这就是 CPU 架构支持范围在产物层面的体现。

**快速安装文档入口**（docker 部署、手动安装、离线编译等细节都在这里）：

[README.md:86-89](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L86-L89)

#### 4.3.4 代码实践

**实践目标**：验证一台（真实或假想的）机器是否在 oam-tools 支持范围内，并写出对应的 CANN ops 包代号。

**操作步骤**：

1. 在有昇腾设备的环境执行：

   ```bash
   npu-smi info
   uname -m
   ```

2. 从 `npu-smi info` 输出中找到 Name 列，对照 [README.md:93-97](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/README.md#L93-L97) 的表格判断：Name 是否包含 `910B`、`910_93`、`950` 之一。
3. 记录 `uname -m` 的输出（`aarch64` 或 `x86_64`），推断编译产物应为 `cann-oam-tools_<版本>_linux-<该值>.run`。

**需要观察的现象**：

- `npu-smi info` 输出的芯片名可能是 `910B2` 这类带子型号的字符串——注意按"包含 910B 关键字"匹配，而不是全字符串相等。
- `uname -m` 输出应与 README 声称的两种架构之一吻合。

**预期结果**：得出"芯片是否支持 + 对应 ops 包代号 + 产物架构后缀"三项结论。若在无设备环境（本讲义的编写/阅读环境通常如此），无法真实执行 `npu-smi info`，请直接在纸上用 README 表格完成一次模拟判断（例如：Name 列为 `910B3` → 支持，代号 `910b`），并标注"待本地验证"。

#### 4.3.5 小练习与答案

**练习 1**：`npu-smi info` 显示某卡 Name 为 `910B4`，该机器是否被支持？对应哪个 CANN ops 包代号？

**参考答案**：支持。README 明确说明 `npu-smi info` 可能显示带子型号的字符串（如 `910B1`/`910B2`/`910B3`/`910B4`），按"Name 列包含 `910B` 关键字"匹配即可，对应 CANN ops 包代号为 `910b`（Atlas A2 训练系列 / Atlas 800I A2 推理产品）。

**练习 2**：同事说要装"910C 的 CANN 包"，包名应该怎么写？

**参考答案**：包名中不应出现 `910c`。"910C"是商用别称，对应表中 `910_93`（Atlas A3 系列）；自 CANN 8.5.0 起 ops 包统一命名为 `Ascend-cann-A3-ops_*`，即代号为 `A3`。

**练习 3**：oam-tools 支持哪些 CPU 架构？这个信息在哪里能直接看出来？

**参考答案**：支持 `aarch64` 和 `x86_64`。直接体现是编译产物命名 `cann-oam-tools_<cann_version>_linux-<arch>.run`，其中 `<arch>` 只能取这两个值（README.md 第 133 行）。

## 5. 综合实践

**任务：制作一张《oam-tools 全景速查卡》并完成一次"虚拟接单"。**

把你在本讲三个模块里学到的内容合成一份单页速查材料，包含四块：

1. **项目定位区**：三行以内说清 OAM-Tools 是什么（提示：CANN 运维工具集、故障定位 + 性能调优、四大组件共享构建体系）。
2. **组件对照区**：完成 4.2.4 的四列对照表（组件 / 解决什么问题 / 典型命令 / 典型使用场景），场景必须来自 `docs/zh/` 下各组件 README 链接到的具体文档，不许凭空编。
3. **硬件支持区**：抄录三行芯片对照表，并补充两条"易错点备注"（910B 子型号匹配规则、910C 别称与 A3 包名）。
4. **虚拟接单区**：给自己出三个"工单"，每个工单只写"应该用哪个组件、什么命令"，不要求真实执行：
   - 工单 A：训练任务报 AI Core Error，有一个已生成的报告目录要解析；
   - 工单 B：想确认 0 号卡健康状态，并把当前环境运维信息打包；
   - 工单 C：怀疑两卡之间 allreduce 带宽低于预期，要量化测一下。

完成自查标准：三个工单分别命中 msaicerr（`-p` 模式）、asys（`health` + `collect`）、hccl_test，命令格式与 README 第 183-268 页段的示例一致。这份速查卡建议保存在 `oam-tools-tutorial/` 之外的个人笔记里，本仓库目录只放讲义。

## 6. 本讲小结

- OAM-Tools 是华为 CANN 的开源运维工具集，围绕**故障定位**与**性能调优**两大能力，覆盖故障采集、AI Core Error 分析、性能采集分析、集合通信测试的完整运维链路。
- 四大组件各司其职：**asys**（Python，一键故障信息采集与诊断，8 个子命令）、**msaicerr**（Python，AI Core Error 报告/Dump 解析与环境检查，`-p`/`-d`/`-e` 三种模式）、**msprof**（C++ collector + Python 分析 wheel，多种采集方式）、**hccl_test**（C++，集合通信正确性与性能测试）。
- 所有组件共享 CMake + build.sh 构建体系，打成 `.run` 包后释放到 CANN 安装目录 `tools/` 子目录下。
- 支持的昇腾处理器为 `910B`（含子型号，ops 包代号 `910b`）、`910_93`（Atlas A3，业内"910C"，代号 `A3`）、`950`（代号 `950`）；CPU 架构支持 `aarch64` 与 `x86_64`。
- 仓内文档按组件分目录：`docs/zh/asys/`（最细，19 篇）、`docs/zh/msaicerr/`、`docs/zh/profiling/`（注意目录名不是 msprof，且带芯片条件标记）、`docs/zh/hccl_test/`；官方完整指南见 README「相关文档」一节的四个跳转链接。

## 7. 下一步学习建议

下一讲 **u1-l2《构建与打包：build.sh 与 CMake 体系》**将进入第一段真实脚本代码：`build.sh` 如何解析参数、`CMakeLists.txt` 如何组织四个组件的构建、第三方库与闭源 bundle 如何自动下载。建议在此之前：

- 预读 `build.sh` 开头的参数帮助部分（执行 `bash build.sh -h`，或在仓库里直接阅读脚本）。
- 预读 `docs/zh/quick_install.md`，了解环境准备与 docker 编译方式——如果你没有昇腾设备，docker 是跟完本手册构建类讲义的主要途径。

如果你已经迫不及待想看组件源码，也可以先跳到 u1-l3《目录结构与入口文件地图》，在那里我们会定位 `asys.py`、`msaicerr.py`、msprof collector 和 hccl_test 的入口文件；但构建体系（u1-l2）是后续所有"跑起来"类实践的前提，建议不要跳过。
