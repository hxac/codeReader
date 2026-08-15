# 无卡调试：NPU Simulator 仿真开发调试

## 1. 本讲目标

本讲是调试调优单元（u8）的收官篇。u8-l1 解决「算错了怎么查」（printf / DumpTensor），u8-l2 解决「有卡时怎么测性能」（msprof op），本讲解决最后一类问题：**手边没有 NPU 真机，或者需要指令级的流水细节时，怎么开发和调试算子**。

学完本讲你应该能够：

1. 说出 NPU Simulator（npusim）的定位、适用场景与硬性限制（支持哪些芯片、不支持哪类算子）。
2. 掌握无真实硬件时的完整仿真流程：编译算子 → 链接仿真运行时 → 仿真执行 → 生成流水报告。
3. 理解 `build.sh --simulator` 背后的实现机制（仿真库符号链接与 `LD_LIBRARY_PATH` 替换）。
4. 能读懂 `trace_core*.json` 指令流水图中的 VECTOR / MTE 等字段，据此判断算子瓶颈在搬运还是计算。

## 2. 前置知识

在阅读本讲前，你需要了解以下概念（均在前序讲义建立过，这里做一句话复习）：

- **Host 侧与 Device 侧**：Host 指 x86 服务器上的 CPU 程序（aclnn 调用、tiling 计算），Device 指 AI Core 上的 kernel 执行。见 u2-l1。
- **runtime 库**：算子样例可执行文件运行时要加载 `libruntime.so`、`libascend_hal.so` 等动态库，正常情况下这些库通过驱动和固件与真实 NPU 通信。
- **仿真（simulation）**：用软件模型模拟芯片行为。本讲遇到的两种仿真器形态不同：
  - **Kernel UT 的 tikicpulib 仿真**（u7-l2）：把 Ascend C kernel 编译成 x86 主机代码，测逻辑正确性，**不产生性能数据**。
  - **本讲的 SoC 级仿真（NPU Simulator / npusim）**：在 Host 上模拟整颗芯片的指令执行，kernel 二进制与真机兼容，**能输出指令流水图和精度对账结果**。
- **指令流水（pipeline）**：AI Core 内部有多条并行工作的流水（向量、标量、矩阵、搬运），性能调优的本质是让这些流水尽量不空转。见 u8-l2。
- **soc_version 与架构代号**：如 `ascend910b` 对应架构 `2201`、`ascend950` 对应 `3510`，本讲会看到 build.sh 用 `dav_<架构号>` 组织仿真库目录。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `docs/zh/debug/npu_sim.md` | NPU Simulator 官方使用文档：工具定位、使用约束、`npusim record` / `npusim report` 命令说明、流水图字段解释 |
| `build.sh` | 仓库构建入口脚本；其中 `--simulator` 选项与 `build_single_example` 函数实现了「无卡跑样例」的仿真库链接逻辑 |
| `docs/zh/debug/op_debug_prof.md` | 算子性能调优文档；其中「仿真流水图采集」一节给出了 npusim 与 msprof op simulator 的分工（按芯片代际选择） |

## 4. 核心概念与源码讲解

### 4.1 NPU Simulator 是什么：定位、能力与限制

#### 4.1.1 概念说明

NPU Simulator 是一款**面向算子开发场景的 SoC 级芯片仿真工具**，集成在 CANN toolkit 包里。它的核心卖点是：研发人员在**无法获取芯片或芯片资源紧缺**的情况下，也能获得与真实芯片几乎一致的验证效果和性能反馈。

它提供两种能力：

1. **精度仿真**：输出 bit 级精度结果，可以用来做算子的精度验证（相当于免费得到一份 golden 对账）。
2. **性能仿真**：输出指令流水图，用来定位算子性能瓶颈，粒度比 u8-l2 的 msprof op 上板采集更细（细到每条指令在哪条流水上、何时发射）。

关键机制是与板上运行**二进制兼容**：同一份 kernel 既能在仿真器上执行，也能在真实 AI 处理器上执行，不需要为仿真单独编一版代码。这一点与 u7-l2 的 Kernel UT（tikicpulib 把 kernel 编成 x86 代码）有本质区别。

一个容易踩坑的历史沿革：该工具原名 `cannsim`，自 2026 年 7 月 30 日版本起正式更名为 `npusim`，旧命令作为别名保留一段时间。你在旧文档、旧脚本里看到 `cannsim record`，对应的就是本讲的 `npusim record`。

#### 4.1.2 核心流程

无卡仿真的整体流程：

```text
安装 CANN toolkit（无需驱动/固件）
        │
        ▼
source set_env.sh ──► 编译算子包并安装（build.sh --pkg）
        │
        ▼
编译算子调用样例（test_aclnn_xxx 可执行文件）
        │
        ▼
npusim record ./test_aclnn_xxx -s Ascend950 --gen-report
        │                                   │
        ▼                                   ▼
npusim.log（精度结果/程序打印）    report/results/.../trace_core*.json
                                          │
                                          ▼
                            chrome://tracing 打开流水图分析
```

使用约束务必记牢（来自官方文档的「使用约束」一节）：

- 推荐环境：16 核 CPU、32GB 以上内存；不支持 arm 环境。
- 依赖 CANN 软件包，但**无需安装驱动和固件**——这正是「无卡」的含义。
- 仅支持**单卡场景**，代码中只能设置为 0 卡，修改可见卡号会导致仿真失败。
- 仅支持 **AI Core 计算类算子**，不支持 MC2 和 HCCL 类型（通信类）算子。
- 工具本身仅支持 **Ascend950PR 和 Ascend950DT** 芯片。
- 属开发工具，不建议在生产环境使用。

#### 4.1.3 源码精读

工具定位与两大能力，见官方文档开篇：

- [docs/zh/debug/npu_sim.md:1-10](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md#L1-L10)：介绍 NPU Simulator 是 SoC 级芯片仿真工具，明确「二进制兼容」特性，并列出精度仿真与性能仿真两大用途。

使用约束清单：

- [docs/zh/debug/npu_sim.md:14-25](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md#L14-L25)：列出环境配置、权限、单卡限制、仅支持 AI Core 算子、仅支持 Ascend950PR/950DT、不支持 arm 等全部硬性约束。

名称变更通知（cannsim → npusim）：

- [docs/zh/debug/npu_sim.md:27-30](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md#L27-L30)：说明自 2026 年 7 月 30 日起 `cannsim` 更名为 `npusim`，旧命令是别名。

环境准备（集成在 toolkit 中）：

- [docs/zh/debug/npu_sim.md:32-34](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md#L32-L34)：NPU Simulator 集成在 CANN toolkit 包里，按 quick_install 文档安装软件包即可，无需驱动固件。

#### 4.1.4 代码实践

1. **实践目标**：确认本机环境是否具备仿真条件，并自查是否踩到约束红线。
2. **操作步骤**：
   - 在已安装 CANN toolkit 的机器上执行 `source ${ASCEND_HOME_PATH}/set_env.sh`（具体路径按你的安装位置调整）。
   - 执行 `npusim --help`，确认命令存在；如果提示命令不存在，说明 toolkit 版本过旧（还在 `cannsim` 时代）或环境变量未生效，可再试 `cannsim --help`。
   - 检查仿真库目录是否存在：`ls ${ASCEND_HOME_PATH}/*-linux/simulator/`（目录结构细节见 4.3 节）。
3. **需要观察的现象**：`npusim --help` 输出 `record` 与 `report` 两个子命令的说明。
4. **预期结果**：看到与 [docs/zh/debug/npu_sim.md:241-252](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md#L241-L252) 一致的 usage 输出。
5. 若环境中无 CANN toolkit，本步骤**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 NPU Simulator 与 u7-l2 的 Kernel UT 仿真「都能无卡跑 kernel，但不是一回事」？

**参考答案**：Kernel UT 的 tikicpulib 把 Ascend C kernel 编译成 x86 主机代码执行，只验证逻辑与内存安全，不产生性能数据；NPU Simulator 是 SoC 级仿真，kernel 二进制与真机兼容，除精度结果外还能输出指令流水图用于性能分析。前者面向「测得对不对」，后者面向「跑得快不快」加上「算得准不准」。

**练习 2**：你的同事想在仿真环境里调试一个 HCCL 通信算子，可行吗？

**参考答案**：不可行。官方约束明确仿真环境仅支持 AI Core 计算类算子，不支持 MC2 和 HCCL 类型算子。

### 4.2 npusim 命令族：record 执行仿真与 report 生成流水

#### 4.2.1 概念说明

`npusim` 有两个子命令，分工清晰：

- **`npusim record`**：把你的可执行程序「包裹」在仿真环境里跑一遍，录制芯片级行为。它不改你的程序——原样执行 `./test_aclnn_add_example`，只是底层的 runtime/驱动被替换成了仿真模型，因此程序里的 `printf` 输出、精度对账结果都会照常出现。
- **`npusim report`**：对 record 产生的数据目录做二次解析，生成可视化的指令流水图（`trace_core*.json`），供 chrome://tracing 查看。

可以类比 u8-l2 的 msprof：`record` 相当于「采集」，`report` 相当于「解析导出」。如果 record 时带了 `-g/--gen-report`，则采集完自动解析，无需再手动跑 report。

#### 4.2.2 核心流程

`npusim record [options] user_app` 的关键参数（完整表见引用源码）：

| 参数 | 必选/可选 | 含义 |
| --- | --- | --- |
| `-s` / `--soc-version` | 必选 | 模拟目标芯片版本，如 `Ascend950` |
| `-o` / `--output` | 可选 | 输出目录，默认当前目录 |
| `-g` / `--gen-report` | 可选 | 仿真完自动解析并生成分析报告 |
| `-u` / `--user-option` | 可选 | 透传给算子程序的用户自定义参数 |
| `-n` / `--core-id` | 可选 | 启用日志的 AI Core，格式 `'all'`、`'0-2,12-14'`、`'5'`；配合 `-g` 且未指定时回退到 core 0 |
| `user_app` | 必选 | 待运行的程序，`./app`、`python train.py`、`bash run.sh` 都可以 |

执行流程：

```text
npusim record ./test_aclnn_add_example -s Ascend950 --gen-report
        │
        ├─► 生成 npusim_{timestamp}_{user_app}/ 目录
        │        └── npusim.log            ← 程序打印 + 精度对账结果
        │
        └─► (-g 时) 自动解析生成
                 └── report/results/kernel_*/core_*/
                          ├── trace_core0.json  ← 指令流水图（Chrome tracing 格式）
                          └── ...
```

精度对账结果在 `npusim.log` 里，形如：

```text
['case_name', 'wrong_num', 'total_num', 'result', 'task_duration']
[' case001', 0, 65536, 'Success']
```

`wrong_num=0` 即 bit 级一致。

`npusim report -e <npusim_目录>` 则可对已录制的目录补生成流水图，`-n` 指定生成哪些核（不指定默认只生成 0 核）。

#### 4.2.3 源码精读

快速开始全流程（编译 → 安装 → 仿真 → 看流水）：

- [docs/zh/debug/npu_sim.md:40-59](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md#L40-L59)：给出 add_example 的仿真标准流程——先用 `build.sh --pkg --soc=ascend950 --vendor_name=custom --ops=add_example` 编译并安装 run 包，编出 `test_aclnn_add_example`，再执行 `npusim record ./test_aclnn_add_example -s Ascend950 --gen-report`。

仿真日志位置与程序输出示例：

- [docs/zh/debug/npu_sim.md:59-71](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md#L59-L71)：日志在 `examples/add_example/examples/build/bin/npusim_*/npusim.log`，样例中每行 `result[i] is: 2.000000` 的打印都会照常出现在仿真日志里。

流水文件位置与打开方式：

- [docs/zh/debug/npu_sim.md:73-81](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md#L73-L81)：流水文件在 `npusim_*/report/results/kernel_*/core_*/trace_core0.json`，用 Chrome 打开 `chrome://tracing` 后把 json 拖入即可。

record 参数表：

- [docs/zh/debug/npu_sim.md:95-104](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md#L95-L104)：`-s/-o/-g/-u/-n` 与 `user_app` 的完整说明，注意 `-s` 是唯一必选项。

输出目录结构与精度对账示例：

- [docs/zh/debug/npu_sim.md:119-135](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md#L119-L135)：record 生成 `npusim_{timestamp}_${user_app}/npusim.log`；精度比较结果展示 `wrong_num/total_num/result`，全部正确即为 `Success`。

report 命令参数与示例：

- [docs/zh/debug/npu_sim.md:149-179](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md#L149-L179)：`npusim report -e <目录> [-o 输出] [-n '0-1, 11-12']`，产出 `trace_core0.json`、`trace_core1.json` 等文件。

帮助命令：

- [docs/zh/debug/npu_sim.md:210-222](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md#L210-L222)：`npusim --help`、`npusim record --help`、`npusim report --help` 可查各子命令完整参数。

#### 4.2.4 代码实践

1. **实践目标**：走通一次「无卡仿真执行」，亲眼看到样例打印出现在仿真日志里。
2. **操作步骤**（在有 CANN toolkit、无 NPU 卡的环境）：
   - 编译并安装算子包：`bash build.sh --pkg --soc=ascend950 --vendor_name=custom --ops=add_example`，然后安装 `./build_out/cann-ops-nn-custom_linux-<arch>.run`。
   - 编译样例可执行文件（可用 4.3 节的 `--run_example --simulator` 一步完成，或参考 [docs/zh/invocation/quick_op_invocation.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/invocation/quick_op_invocation.md) 手工 g++ 编译 `test_aclnn_add_example.cpp`）。
   - 执行：`npusim record ./test_aclnn_add_example -s Ascend950 --gen-report`。
   - 打开 `npusim_*/npusim.log` 查看打印。
3. **需要观察的现象**：日志中出现 `add_example first input[0] is: 1.000000, ..., result[0] is: 2.000000` 这样的输出，同时目录下生成 `report/` 子目录。
4. **预期结果**：打印内容与真机执行一致（仿真对算子语义透明），流水文件 `trace_core0.json` 已生成。
5. 本实践依赖 950 系列的 toolkit 与仿真库，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：record 时已经带了 `-g`，还需要再执行 `npusim report` 吗？

**参考答案**：不需要。`-g/--gen-report` 表示仿真完成后自动解析并生成分析报告；`npusim report` 是给「录制时没带 `-g`」的场景做补解析用的（或需要换 `-n` 核范围重新生成时使用）。

**练习 2**：`npusim record` 的 `-u` 参数和 `user_app` 参数有什么区别？

**参考答案**：`user_app` 是必选的待运行程序本身（如 `./test_aclnn_add_example`）；`-u/--user-option` 是可选的、以命令行选项形式透传给该程序的自定义参数。

### 4.3 build.sh 的 `--simulator` 集成：仿真库链接机制

#### 4.3.1 概念说明

前两节是「手动挡」：自己编可执行文件、自己调 npusim。ops-nn 仓库还提供了「自动挡」——`build.sh --run_example` 支持 `--simulator` 选项，在编译并运行样例前自动把运行时环境切到仿真模式。

它的原理值得精读，因为这是理解「无卡怎么能跑 NPU 程序」的钥匙：样例可执行文件运行时会通过动态链接加载 `libruntime.so` 和 `libascend_hal.so`（runtime 与驱动适配层）。真机上这两个库走驱动访问硬件；CANN toolkit 里另有一对**仿真替身库** `libruntime_camodel.so`、`libnpu_drv_camodel.so`（camodel = chip area model，芯片模型），接口相同、实现指向软件仿真器。build.sh 做的事情就是：把这对替身库**软链接成真身库的名字**，并通过 `LD_LIBRARY_PATH` 让样例优先加载它们——程序无感知，底层已是仿真器。

#### 4.3.2 核心流程

`build_single_example` 函数中仿真分支的执行逻辑：

```text
--run_example xxx eager cust --vendor_name=custom --simulator
        │
        ├─ 1. EXAMPLE_MODE == "graph"？ ──是──► 报错退出（不支持图模式）
        │
        ├─ 2. 取第一个 COMPUTE_UNIT（默认 ascend910b），
        │      查 SOC_TO_ARCH 表得架构号（如 ascend950 → 3510）
        │      查不到 ──► 报错退出
        │
        ├─ 3. SIMULATOR_PATH = ${ASCEND_HOME_PATH}/${ARCH_INFO}-linux/simulator/dav_${架构号}/lib
        │      检查 libruntime_camodel.so、libnpu_drv_camodel.so 是否存在
        │      缺任一 ──► 报错退出
        │
        ├─ 4. rm -fr ${BUILD_PATH}/simulator 并重建，
        │      ln -sf libruntime_camodel.so  → libruntime.so
        │      ln -sf libnpu_drv_camodel.so → libascend_hal.so
        │
        └─ 5. export LD_LIBRARY_PATH=${BUILD_PATH}/simulator:${SIMULATOR_PATH}:...
               之后照常 g++ 编译样例、直接运行可执行文件
```

注意最后一步：开启 `--simulator` 后，样例是**直接执行**的（不走 `npusim record` 包裹），因为加载的 runtime 本身已是仿真模型。这种方式适合快速验证功能；若要流水图，仍需用 4.2 节的 `npusim record --gen-report`。

soc 到架构的映射关系（build.sh 顶部的关联数组）：

| soc_version | 架构号 |
| --- | --- |
| ascend310b | 3002 |
| ascend310p | 2002 |
| ascend910_93 / ascend910b | 2201 |
| ascend950 / ascend350 | 3510 |
| ascend910 | 1001 |
| mc62 | 5102 |

即仿真库在 toolkit 中按 `simulator/dav_2201`、`dav_3510` 等目录分架构存放。

#### 4.3.3 源码精读

soc → 架构映射表定义：

- [build.sh:14-17](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L14-L17)：`SOC_TO_ARCH` 关联数组声明各 soc_version 对应的架构号，仿真库路径、编译目标目录都依赖这张表。

`--simulator` 作为合法长选项注册：

- [build.sh:26-30](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L26-L30)：`SUPPORTED_LONG_OPTS` 列表中包含 `"simulator"`，是参数解析的准入声明。

帮助文档中的用法示例：

- [build.sh:334-347](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L334-L347)：`run_example` 帮助信息，最后一行示例 `bash build.sh --run_example mat_mul_v3 eager cust --vendor_name=custom --simulator` 展示了仿真跑样例的完整命令形态——`--simulator` 挂在 `--run_example` 尾部。

默认关闭与选项解析：

- [build.sh:741](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L741)：`ENABLE_SIMULATOR=FALSE` 初始化默认值。
- [build.sh:933](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L933)：`simulator) ENABLE_SIMULATOR=TRUE ;;` 解析到 `--simulator` 时置位开关。

仿真分支主体（本模块核心，建议逐行读）：

- [build.sh:1509-1513](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1509-L1513)：图模式（`EXAMPLE_MODE == "graph"`）直接判错退出——`--simulator` 只支持 eager（aclnn）方式跑样例。
- [build.sh:1514-1524](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1514-L1524)：取第一个编译目标单元（未指定时默认 `ascend910b`），查 `SOC_TO_ARCH` 得架构号，查不到报错退出。
- [build.sh:1525-1533](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1525-L1533)：拼出 `SIMULATOR_PATH="${ASCEND_HOME_PATH}/${ARCH_INFO}-linux/simulator/dav_<架构号>/lib"`，逐一检查 `libruntime_camodel.so` 与 `libnpu_drv_camodel.so` 是否存在，缺失即报错——这也是「toolkit 没装仿真组件」时的第一现场。
- [build.sh:1534-1539](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1534-L1539)：清理并重建 `${BUILD_PATH}/simulator` 目录，把两个 camodel 库分别软链为 `libruntime.so`、`libascend_hal.so`，然后把该目录与 `SIMULATOR_PATH` 前置到 `LD_LIBRARY_PATH`——样例后续加载的「runtime 与驱动」就换成了仿真替身。

之后的编译与直接执行：

- [build.sh:1562-1565](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1562-L1565)：cust 模式下用 g++ 链接 `libcust_opapi` 等库编出 `test_aclnn_<example>`，与真机路径完全相同。
- [build.sh:1581-1587](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/build.sh#L1581-L1587)：`${BUILD_PATH}/"${pattern}${example}" | tail -n 10` 直接运行样例可执行文件并取最后 10 行输出——由于 LD_LIBRARY_PATH 已指向仿真库，这次执行落在仿真器上。

按芯片代际选择仿真工具的官方分工：

- [docs/zh/debug/op_debug_prof.md:154-193](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/op_debug_prof.md#L154-L193)：性能调优文档明确——Ascend 950PR 用 NPU Simulator（`npusim record ... --gen-report`）；Atlas A2/A3 系列（ascend910b/ascend910_93）用 msprof 的仿真命令 `msprof op simulator --kernel-name "AddExample" ./test_aclnn_add_example`，流水文件为 `OPPROF**/simulator/visualize_data.bin`，用 MindStudio Insight 查看。

#### 4.3.4 代码实践

1. **实践目标**：用 build.sh 的「自动挡」在无卡环境跑通 add_example，并理解 `build_out/.../simulator/` 目录里发生了什么。
2. **操作步骤**：
   - 确认已安装含仿真组件的 CANN toolkit 并 source 环境变量。
   - 先按常规流程编译安装算子包：`bash build.sh --pkg --soc=ascend950 --vendor_name=custom --ops=add_example` 并安装 run 包。
   - 执行：`bash build.sh --run_example add_example eager cust --vendor_name=custom --simulator --soc=ascend950`。
   - 命令结束后检查 `${BUILD_PATH}/simulator/`（`build_out` 下对应样例目录内）的两个软链接指向。
   - 与不带 `--simulator` 的同命令（真机环境）输出对比。
3. **需要观察的现象**：
   - 终端先打印 `[INFO] Successfully linked simulator libraries: ...libruntime_camodel.so, ...libnpu_drv_camodel.so`；
   - 随后样例正常输出 `add_example ... result[i] is: 2.000000`；
   - `simulator/` 目录下 `libruntime.so → .../libruntime_camodel.so`、`libascend_hal.so → .../libnpu_drv_camodel.so`。
4. **预期结果**：功能输出与上板执行一致（二进制兼容），差异只在耗时（仿真慢得多）与执行环境（无 `/dev/davinci*` 设备依赖）。
5. 无卡/有卡双环境对比部分**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `--simulator` 与 `--run_example xxx graph`（图模式）不能同时使用？

**参考答案**：build.sh 的 `build_single_example` 在仿真分支开头显式判断 `EXAMPLE_MODE == "graph"` 即报 usage 退出（build.sh:1509-1513）。`--simulator` 的机制是替换 runtime/驱动动态库，这条路径只覆盖 aclnn eager 调用；GE 图模式依赖 ge_runner 等更多组件，不在该仿真路径支持范围内。

**练习 2**：若执行时报错 `.../simulator/dav_3510/lib/libruntime_camodel.so not found`，最可能的原因是什么？

**参考答案**：CANN toolkit 中缺少对应架构（3510，即 ascend950/ascend350 代际）的仿真组件——可能是 toolkit 版本不含仿真库、安装时未勾选相关组件，或 `--soc` 传错导致查到了不存在的架构目录。可检查 `ls ${ASCEND_HOME_PATH}/*-linux/simulator/` 确认已安装哪些 `dav_*` 架构。

**练习 3**：`build.sh --simulator` 方式和 `npusim record` 方式都能无卡跑样例，二者关系是什么？

**参考答案**：`build.sh --simulator` 通过软链 camodel 库 + LD_LIBRARY_PATH 让样例「直接运行」在仿真 runtime 上，适合快速功能验证；`npusim record` 是工具化包裹执行，除运行外还录制数据并（配合 `--gen-report`）生成指令流水图用于性能分析。前者是仓库构建脚本对后者的运行时机制的简化利用。

### 4.4 仿真流水图解读：用 trace_core*.json 定位性能瓶颈

#### 4.4.1 概念说明

流水图是仿真性能分析的最终产出。文件是 Chrome tracing 格式的 json，查看方式零门槛：Chrome 地址栏输入 `chrome://tracing`，把 `trace_core0.json` 拖进去即可。时间轴横向展开，每条泳道对应 AI Core 内的一条流水/单元，快捷键 W（放大）、S（缩小）、A（左移）、D（右移）。

与 u8-l2 的上板流水占比数据相比，仿真流水图的价值在于**指令级细节**：你能看到每个时间点哪条流水在忙、哪条在等待，从而判断瓶颈是「搬运供不上数据」还是「计算排布有空洞」。

#### 4.4.2 核心流程

读图的基本方法（结合 u5 单元的知识）：

```text
1. 找 MTE2/MTE3 泳道（GM ↔ 片上搬运）
   ├─ MTE2 长时间忙、VECTOR 大段空闲 → 访存受限
   │     （elementwise 算子典型形态，对应 u8-l2 的"搬运占比高"）
   └─ VECTOR 持续有活、MTE 间歇空闲 → 计算受限
2. 找 FLOWCTRL / ICACHELOAD
   ├─ ICACHELOAD 出现未命中 → 指令过多或排布稀疏
3. 对照双缓冲（u5-l1）：搬运与计算的时间段若未重叠，
   说明双缓冲未起效（BUFFER_NUM 配置或 tiling 切分问题）
```

#### 4.4.3 源码精读

流水字段官方定义表（读图的字典，建议收藏）：

- [docs/zh/debug/npu_sim.md:185-198](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md#L185-L198)：各泳道含义——VECTOR（向量运算单元）、SCALAR（标量运算单元）、Cube（矩阵乘运算单元）、MTE1（L1 → L0A/L0B/UBUF 搬运）、MTE2（DDR/GM/L2 → L1/L0A·B/UBUF 搬运）、MTE3（UBUF → DDR/GM/L2/L1 搬运）、FIXP（L0C → OUT/L1）、FLOWCTRL（控制流）、ICACHELOAD（ICache 未命中）。

查看方式与快捷键：

- [docs/zh/debug/npu_sim.md:181-183](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md#L181-L183)：`chrome://tracing` 拖入 json 文件打开，W/S/A/D 控制缩放平移。

针对核选择：

- [docs/zh/debug/npu_sim.md:103](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md#L103) 与 [docs/zh/debug/npu_sim.md:157](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/npu_sim.md#L157)：`-n/--core-id` 支持 `'all'`、`'0-2,12-14'`、`'5'` 三种写法，report 不指定时默认只生成 0 核——多核 kernel（BlockDim > 1）要记得显式指定核号或 `all`。

#### 4.4.4 代码实践

1. **实践目标**：读懂 add_example 的仿真流水图，判断它是访存受限还是计算受限。
2. **操作步骤**：
   - 在 4.2.4 实践产生的 `npusim_*/report/results/kernel_*/core_*/` 下找到 `trace_core0.json`。
   - Chrome 打开 `chrome://tracing`，拖入该文件。
   - 分别观察 MTE2（搬入）、VECTOR（Add 计算）、MTE3（搬出）三条泳道的时间分布。
3. **需要观察的现象**：VECTOR 泳道每次只有很短的一段计算脉冲，大部分时间线被 MTE2/MTE3 的搬运块占据，且搬运与计算的时间段基本衔接而非重叠。
4. **预期结果**：add_example 属于典型 elementwise 算子，流水图应呈现「访存为主、计算占比极小」的形态，与 u8-l2 中「elementwise 算子典型为访存受限」的结论互相印证；优化方向是 ubFactor、双缓冲与 32 字节对齐三件套（见 u4-l1、u5-l1、u5-l2）。
5. 具体泳道形态随 toolkit 版本略有差异，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：流水图中 MTE2 和 MTE3 分别负责什么方向的数据搬运？对应 add_example 三段式流水（u5-l1）的哪一段？

**参考答案**：MTE2 负责 {DDR/GM, L2} → {L1, L0A/B, UBUF} 方向的搬运，对应 CopyIn（GM → UB）；MTE3 负责 UBUF → {DDR/GM, L2, L1} 方向，对应 CopyOut（UB → GM）。

**练习 2**：一个 matmul 类 kernel 的流水图里，除了 VECTOR/MTE 外还应重点关注哪条泳道？

**参考答案**：Cube（矩阵乘运算单元）泳道——Cube 类算子的主要计算发生在这里；此外 FIXP 泳道（L0C → OUT/L1 搬运）与 MTE1（L1 → L0A/L0B/UBUF）也是 Cube 流水链路的组成部分。

**练习 3**：为什么说仿真流水图比 u8-l2 的 msprof op 上板数据「更细」？代价是什么？

**参考答案**：上板采集（OPPROF 流水占比文件）给出各类流水的占比汇总；仿真流水图给出指令级的逐时间段视图（哪条指令、哪条流水、何时发射），能定位到具体的排布空洞。代价是仿真执行速度远慢于真机，且仅支持特定芯片（950PR/950DT 的 npusim，或 A2/A3 的 msprof op simulator）与 AI Core 计算类算子。

## 5. 综合实践

**任务：完成 add_example 的「仿真 ↔ 上板」双环境对照记录**（无卡环境可只完成仿真侧）。

1. **准备**：按 u1-l2/u8-l2 的流程，在配套环境编译安装算子包：`bash build.sh --pkg --soc=ascend950 --vendor_name=custom --ops=add_example`，安装 run 包。
2. **仿真侧（自动挡）**：执行 `bash build.sh --run_example add_example eager cust --vendor_name=custom --simulator --soc=ascend950`，记录：
   - 终端是否出现 `Successfully linked simulator libraries` 日志；
   - `${BUILD_PATH}/simulator/` 下两个软链接的实际指向；
   - 样例最后 10 行输出。
3. **仿真侧（工具挡）**：用 `npusim record ./test_aclnn_add_example -s Ascend950 --gen-report` 重新执行，记录：
   - `npusim.log` 中的精度对账表（`wrong_num/total_num/result`）；
   - `report/.../core_0/trace_core0.json` 中 MTE2/VECTOR/MTE3 泳道的忙闲占比（目测即可）。
4. **上板侧（如有真机）**：同一份样例不带 `--simulator` 直接跑，记录输出与 u8-l2 的 msprof op Task Duration。
5. **产出一份对照笔记**，回答三个问题：
   - 仿真与上板的功能输出是否完全一致？（预期：一致，二进制兼容）
   - 仿真与上板的性能数据能否直接对比数值？（预期：不能直接比绝对值，仿真用于看流水形态与相对瓶颈）
   - add_example 的瓶颈在搬运还是计算？（预期：搬运，依据是流水图泳道占比）

通过这个任务，你把本讲的四个模块（工具定位、npusim 命令、build.sh 集成、流水图解读）串成了一条完整的无卡开发调试链路，并与 u8-l1/u8-l2 的有卡手段形成互补。

## 6. 本讲小结

- NPU Simulator（npusim，原名 cannsim）是集成在 CANN toolkit 中的 SoC 级芯片仿真工具，无需驱动固件即可在无卡环境获得与真机二进制兼容的精度验证与指令级性能反馈；仅支持 Ascend950PR/950DT、单卡 0 卡、AI Core 计算类算子。
- `npusim record -s <soc> --gen-report <程序>` 录制仿真执行，产出 `npusim.log`（含精度对账）与 `npusim_*/report/`；`npusim report -e <目录>` 可补生成指定核的 `trace_core*.json` 流水图。
- `build.sh --run_example <op> eager cust --simulator` 是仓库封装的自动挡：把 toolkit 中 `simulator/dav_<架构号>/lib` 下的 `libruntime_camodel.so`/`libnpu_drv_camodel.so` 软链成 `libruntime.so`/`libascend_hal.so` 并前置 `LD_LIBRARY_PATH`，让样例直接跑在仿真 runtime 上；仅支持 eager 模式。
- 流水图用 `chrome://tracing` 打开，核心泳道：VECTOR/SCALAR/Cube 是计算，MTE1/2/3 是三级搬运，FIXP/FLOWCTRL/ICACHELOAD 辅助判断；MTE 忙、计算闲即访存受限。
- 按芯片选工具：950PR 用 npusim，Atlas A2/A3（ascend910b/910_93）用 `msprof op simulator`，二者输出格式与查看工具不同（tracing json vs MindStudio Insight）。
- 三种「无真机跑 kernel」的方式各有分工：Kernel UT（u7-l2，tikicpulib）测逻辑、npusim/msprof simulator 测精度与指令流水、上板 msprof op（u8-l2）测真实性能。

## 7. 下一步学习建议

本讲完成后，u8 调试调优单元（printf/DumpTensor、msprof op、NPU Simulator）已全部结束，你已具备「功能调试 → 上板性能 → 无卡仿真」的完整工具箱。接下来进入 u9 扩展开发与二次贡献单元：

- **u9-l1 新建算子工程**：用 `--genop` 从零创建算子，把本单元学到的调试调优手段应用到自己的算子上。
- 若你想继续深挖本讲主题，建议阅读：
  - [docs/zh/debug/op_debug_prof.md](https://github.com/gitcode.com/cann/ops-nn/blob/0e2eac83d24a7ec29a0647698ed0defe1ff1f8f0/docs/zh/debug/op_debug_prof.md) 的「仿真流水图采集」一节，对照两种仿真器的完整命令；
  - `examples/add_example/examples/test_aclnn_add_example.cpp`（u1-l4 已读过），思考为什么仿真执行不需要修改样例任何一行代码；
  - CANN 官方文档中关于 MindStudio Insight 的使用说明（用于 A2/A3 仿真流水数据的可视化）。
