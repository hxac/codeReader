# hccl_test 运行与结果解读：正确性校验与性能数据

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立完成 hccl_test 的运行前准备（环境变量、Hostfile、mpirun 启动命令），并理解每个关键命令行参数的含义。
2. 读懂正确性校验的两条主线：输入灌数（`hccl_check_buf_init.cc`）与逐元素比对（`hccl_check_common.cc`），并理解为什么某些场景下校验会被自动关闭。
3. 精确定位输出表格中 `avg_time(us)` 与 `alg_bandwidth(GB/s)` 这两个统计量在源码中的计算位置，并能用输出数据反算验证。
4. 解读一次完整的带宽测试输出，判断结果是 success、failed 还是 NULL，以及为什么。

本讲是 hccl_test 单元的收官讲，承接 u5-l2 的三层类体系（`HcclTest` → `HcclOpBaseTest` → `HcclOpBaseXxxTest`），把视角从「代码怎么组织」转到「工具怎么跑起来、结果怎么来的」。

## 2. 前置知识

- **集合通信与 HCCL**：AllReduce、Broadcast 等集合通信算子让多张 NPU 协同完成同一种计算。HCCL（Huawei Collective Communication Library）是昇腾平台上的集合通信库，hccl_test 就是对它的单算子测试工具。
- **rank（进程/卡）**：每个 MPI 进程绑定一张 NPU，称为一个 rank。`rank_id` 是本进程编号，`rank_size` 是总卡数。这些概念在 u5-l2 的 rootinfo 建链三步曲中已经建立。
- **算法带宽（algorithm bandwidth）**：衡量集合通信性能的常用指标，计算方式是「数据量 ÷ 耗时」。它不考虑具体通信算法实际搬运了多少字节（那是总线带宽 bus bandwidth 的口径），只反映「这份单卡数据量在这么长时间内完成了」。hccl_test 输出的 `alg_bandwidth` 就是算法带宽。
- **event 计时**：在昇腾设备上，`aclrtRecordEvent` 在 stream（任务队列）中打一个时间戳标记，`aclrtEventElapsedTime` 计算两个 event 之间的毫秒差。用 event 计时而不是主机时钟计时，可以排除「Host 发任务」与「Device 执行」之间的异步误差。
- **数值溢出**：int8 只有 8 位，能表示的整数范围是 -128~127。如果 64 张卡各出一个 2 做 Sum 归约，结果是 128，超出 int8 表达范围——此时「期望值」本身就算不准，校验失去意义，工具会主动关闭校验。这是本讲的一个重要设计点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/zh/hccl_test/execution.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/execution.md) | 用户文档：环境变量、Hostfile、启动命令与结果说明 |
| [docs/zh/hccl_test/cmdline_options_desc.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/cmdline_options_desc.md) | 用户文档：mpirun 与工具参数的完整说明 |
| [src/hccl_test/common/src/hccl_test_common.cc](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc) | `HcclTest` 基类实现：命令行解析、参数校验、数据量对齐、环境变量读取 |
| [src/hccl_test/common/src/hccl_check_buf_init.cc](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_check_buf_init.cc) | 输入灌数与期望值生成（本讲核心之一） |
| [src/hccl_test/common/src/hccl_check_common.cc](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_check_common.cc) | 逐元素结果比对（本讲核心之二） |
| [src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc) | `HcclOpBaseTest` 中间层：溢出判定与结果表格打印 |
| [src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc) | AllReduce 测试实现：计时主循环与统计量计算（本讲实践对象） |

## 4. 核心概念与源码讲解

### 4.1 启动方式：环境变量、Hostfile 与 mpirun 命令

#### 4.1.1 概念说明

hccl_test 的二进制不能直接运行——它需要 mpirun 把 N 个进程分布到各节点的 N 张 NPU 上（回忆 u5-l2：rootinfo 建链需要 MPI 广播）。所以「运行 hccl_test」实际是三层组合：

```text
mpirun（MPI 编排层） → 测试二进制（工具层） → HCCL/ACL（通信与设备层）
```

运行前还需要配置三类环境变量：

1. **路径类**：`PATH`、`LD_LIBRARY_PATH` 指向 MPI 与 CANN 的库目录。
2. **HCCL 行为类**：`HCCL_SOCKET_IFNAME`（选网卡）、`HCCL_CONNECT_TIMEOUT`（建链超时）、`HCCL_BUFFSIZE`（通信域缓存大小，性能测试建议调大且大于测试数据量）。
3. **工具辅助类**：`HCCL_TEST_USE_DEVS`（指定用哪几张卡）、`HCCL_TEST_PROFILING`（采集 profiling 数据）。

#### 4.1.2 核心流程

```text
1. 配置环境变量（PATH/LD_LIBRARY_PATH、HCCL_SOCKET_IFNAME 等）
2. （多机）编写 hostfile：每行一个 "节点:进程数"
3. 在 ${INSTALL_DIR}/tools/hccl_test 目录执行：
   mpirun -f hostfile -n <NPU总数> ./bin/all_reduce_test -p 8 -b 8K -e 64M -f 2 -d fp32 -o sum
        │                          │       │    │    │    │    │      │
        │                          │       │    │    │    │    │      └─ 归约操作
        │                          │       │    │    │    │    └─ 数据类型
        │                          │       │    │    │    └─ 增量因子（倍增）
        │                          │       │    │    └─ 最大数据量
        │                          │       │    └─ 最小数据量
        │                          │       └─ 单节点 NPU 数
        │                          └─ 测试二进制及工具参数
        └─ MPI 参数（hostfile、总进程数）
```

`-f 2` 表示数据量按乘法因子 2 递增：8K → 16K → 32K → … → 64M，每个数据量各跑一轮「预热 + 计时 + 校验」。

#### 4.1.3 源码精读

文档侧的启动说明在 [execution.md:L21-L45](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/execution.md#L21-L45)，这段给出了 MPICH 与 Open MPI 两种场景的环境变量模板。

Hostfile 的格式说明在 [execution.md:L117-L153](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/execution.md#L117-L153)：MPICH 用 `节点ip:进程数`，Open MPI 用 `节点名 slots=进程数`；单机场景可不配。

工具辅助环境变量 `HCCL_TEST_USE_DEVS` 与 `HCCL_TEST_PROFILING` 的文档说明在 [execution.md:L95-L112](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/execution.md#L95-L112)。

源码侧，这两个环境变量的读取点在 `HcclTest` 基类中：

- [hccl_test_common.cc:L493-L557](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L493-L557)：`get_env_resource()` 读取 `HCCL_TEST_PROFILING` 与 `HCCL_BUFFSIZE`，做逐字符数字校验后，若开启 profiling 则调用 `aclprofInit`/`aclprofStart` 挂上性能采集（注意：开启 profiling 会影响通信性能，文档也提醒了这一点）。
- [hccl_test_common.cc:L721-L737](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L721-L737)：`get_mpi_proc()` 中解析 `HCCL_TEST_USE_DEVS`，把 `"4,5,6,7"` 拆成设备号列表，本进程按 `proc_rank % dev_ids.size()` 取自己绑定的卡；未设置时默认 `dev_id = proc_rank % npus`。

完整命令示例见 [execution.md:L159-L201](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/execution.md#L159-L201)。

#### 4.1.4 代码实践

1. **实践目标**：对照文档与源码，确认「工具能识别哪些参数、参数如何被消费」。
2. **操作步骤**：
   - 阅读 [cmdline_options_desc.md:L59-L202](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/cmdline_options_desc.md#L59-L202)，列出工具参数清单。
   - 打开 [hccl_test_common.cc:L271-L296](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L271-L296) 的 `build_longopts()`，把每个长选项（`minbytes`、`stepfactor`、`check` 等）与文档逐条对照。
   - 注意 `build_longopts(bool is910_95)` 的分支：Ascend 950 系列暴露 `-a accelerator`，其他芯片暴露 `-z zero_copy` 与 `-s nslb`——这是文档中 `<!-- npu="950" -->` 条件块的源码来源。
3. **需要观察的现象**：文档列出的参数与 `build_longopts` 中的选项一一对应，没有文档有而源码没有的参数。
4. **预期结果**：得出一张「参数 → 文档章节 → 源码解析点」的对照表（本机无昇腾设备时纸面完成即可）。

#### 4.1.5 小练习与答案

**练习 1**：为什么性能测试场景建议调大 `HCCL_BUFFSIZE`？
**答案**：每个通信域默认占 200MB 缓存区；当测试数据量超过 `HCCL_BUFFSIZE` 时通信需要分片中转，可能引起性能下降，所以建议该值大于测试数据量（文档 [execution.md:L79-L91](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/execution.md#L79-L91)）。源码侧 `get_env_resource()` 读取该值，且 `-t 1`（仅统计 Device 耗时）场景下若 `HCCL_BUFFSIZE <= 100` 会自动把 `-t` 重置为 0（[hccl_test_common.cc:L533-L536](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L533-L536)）。

**练习 2**：`mpirun -n` 的数字和工具参数 `-p` 的数字分别是什么含义？
**答案**：`-n` 是全部节点上的进程（NPU）总数；`-p` 是单个节点上参与测试的 NPU 数。源码中 `get_mpi_proc()` 在用户没传 `-p` 时默认取本机设备总数（[hccl_test_common.cc:L713-L716](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L713-L716)）。

### 4.2 命令行参数校验与数据量对齐

#### 4.2.1 概念说明

u5-l2 讲过 `HcclTest` 是三层继承体系的底盘，参数解析就住在这一层。本模块关注两件事：

1. **参数校验**：`-b/-e` 必须为正、`-r` 必须小于 rank_size、`-c` 只能取 0/1/2 等。
2. **数据量对齐**：部分算子要求单卡数据量能被 rank_size 整除，工具会对用户输入做「微调」——这就是文档中「HCCL Test 工具执行时会对部分算子的 -b、-e、-i 参数所输入的数据量进行地址对齐或 rank size 倍数的微调」这句话的源码出处。理解它能解释「为什么输出表里的 data_size 和我传的 -b 不完全一样」。

#### 4.2.2 核心流程

```text
parse_cmd_line（getopt_long 循环）
    ↓
check_cmd_line → check_data_count
    ├─ 校验 min/max 合法性
    ├─ 未配置步长时：默认步长 = (max - min) / 10
    ├─ need_ranksize_alignment 时：
    │     align_size = rank_size × 512
    │     数据量 ≤ BUF_ALGIN_LINE → 对齐到 rank_size 的倍数
    │     数据量 > BUF_ALGIN_LINE → 对齐到 align_size 的倍数
    │     步长向上取整到 rank_size 的倍数
    └─ -f 与 -i 同时配置 → 告警，-f（乘法因子）优先
```

#### 4.2.3 源码精读

参数的逐项校验集中在 [hccl_test_common.cc:L423-L491](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L423-L491) 的 `check_cmd_line()`：dtype/op 不合法、`warmup_iters`/`iters` 为负、`root_rank` 越界、`check` 不在 0~2、`npus` 超出本机设备数等，每项失败都打印明确的错误提示并返回 -1。

数据量对齐逻辑在 [hccl_test_common.cc:L325-L394](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L325-L394) 的 `check_data_count()`，其中对齐部分是 [L357-L383](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L357-L383)：这段代码用 lambda `align_bytes` 把 min/max 数据量向下对齐，步长向上对齐到 rank_size 的倍数——注意这是「向下取整」，所以对齐后的数据量只会变小或不变。

`-i` 与 `-f` 同时设置时的优先级告警在 [hccl_test_common.cc:L390-L392](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L390-L392)：乘法因子优先，与文档 [cmdline_options_desc.md:L91-L97](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/cmdline_options_desc.md#L91-L97) 的说法一致。

单选项的分发在 [hccl_test_common.cc:L585-L650](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L585-L650) 的 `parse_opt()`，每个 case 对应一个参数的赋值。

#### 4.2.4 代码实践

1. **实践目标**：验证数据量对齐行为。
2. **操作步骤**：阅读 `check_data_count()`，手算一个例子——假设 `rank_size=8`、`-b 1000 -e 1000 -i 0`，且测试的算子 `need_ranksize_alignment` 为真：1000 < 8×512，对齐到 8 的倍数 → 1000 变为 992。
3. **需要观察的现象**：输出表第一列 `data_size` 是 992 而不是 1000。
4. **预期结果**：纸面推导得出 992；有环境时用 `mpirun -n 8 ./bin/all_reduce_test -b 1000 -e 1000 -i 0 ...` 实测验证（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`-b 8K -e 64M -f 2` 中，`8K` 是怎么被解析成数字的？
**答案**：`parse_opt` 的 `case 'b'` 调用 `parsesize()`（[hccl_test_common.cc:L117-L147](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L117-L147)），`strtol` 先取出数字部分，再按尾部字母 K/M/G 乘以对应单位（KILO/MEGA/GIGA）。

**练习 2**：`-c` 参数的三个取值分别是什么行为？
**答案**：0 关闭校验（输出 NULL）；1 开启校验但不输出详细错误（默认）；2 开启校验并打印首个错误位置的期望值与实际值（见 4.3 模块的 `check_level >= 2` 分支）。

### 4.3 正确性校验：灌数、期望值与逐元素比对

#### 4.3.1 概念说明

hccl_test 判断「通信结果是否正确」的思路非常朴素：

1. **灌数**：每个 rank 把输入 buffer 填成同一个固定值（基类里 `val = 2`）。
2. **算期望值**：集合通信的结果是可预测的。例如 8 卡 Sum 归约、每卡输入全 2，则期望输出全 `2 × 8 = 16`。
3. **比对**：把 Device 上的输出拷回 Host，逐元素与期望值比对，任何位置不一致即失败。

这套逻辑分布在三个文件中，靠**函数指针表按 dtype 分发**串联：

| 步骤 | 入口函数 | 位置 |
| --- | --- | --- |
| 输入灌数 | `hccl_host_buf_init` | hccl_check_buf_init.cc |
| 期望值生成（归约类） | `hccl_reduce_check_buf_init` | hccl_check_buf_init.cc |
| 逐元素比对 | `check_buf_result_*` 系列 | hccl_check_common.cc |

#### 4.3.2 核心流程

以 8 卡 fp32 Sum AllReduce 为例：

```text
每卡:
  host_buf 全部填 val=2                     (hccl_host_buf_init)
  check_buf 全部填 val*rank_size = 16       (hccl_reduce_check_buf_init, SUM 分支)
  拷 host_buf → Device send_buff
  预热 warmup_iters 轮 + 计时 iters 轮 HcclAllReduce
  重灌输入，单跑一次 HcclAllReduce            ← 校验用这一次，与计时轮分离
  recv_buff 拷回 Host
  逐元素: |check_buf[i] - result[i]| 是否超阈  (check_buf_result_float)
  有错 → check_err++ → 输出 failed；无错 → success
```

三个关键设计：

- **计时与校验分离**：计时轮跑完后重新灌数、单独再跑一次用于校验，保证校验的那次结果不被多轮复用污染。
- **浮点用相对误差**：浮点数不追求逐位相等，而是相对误差超过阈值（`HCCL_EPSION_FLOAT * 100`）才算错。
- **溢出自动关校验**：卡数太多导致期望值本身溢出时，直接 `check = 0`，输出 NULL 而不是误报 failed。

#### 4.3.3 源码精读

**（1）输入灌数**：`hccl_host_buf_init` 是按 dtype 查表分发的入口，[hccl_check_buf_init.cc:L115-L122](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_check_buf_init.cc#L115-L122) 中 `functionMap` 以 `HcclDataType` 枚举为键找到具体灌数函数。函数表本体在 [hccl_check_buf_init.cc:L699-L716](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_check_buf_init.cc#L699-L716)，覆盖 fp32/int8/fp16/bfp16 及 fp8 系列共 16 种类型。fp32 的实现很简单——整个 buffer 填同一个值（[L27-L36](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_check_buf_init.cc#L27-L36)）。

**（2）期望值生成**：`hccl_reduce_check_buf_init` 同样是查表入口（[hccl_check_buf_init.cc:L364-L371](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_check_buf_init.cc#L364-L371)）。fp32 版本（[L124-L150](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_check_buf_init.cc#L124-L150)）体现了归约数学：

\[ \text{Sum 期望值} = val \times rank\_size, \quad \text{Prod 期望值} = val^{rank\_size}, \quad \text{Min/Max 期望值} = val \]

int8 版本（[L152-L195](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_check_buf_init.cc#L152-L195)）额外做了 `n > 127` 截断到 127 的饱和处理，这是对 int8 溢出的第一道防御。

**（3）逐元素比对**：以浮点为例，[hccl_check_common.cc:L26-L60](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_check_common.cc#L26-L60) 的 `check_buf_result_float()` 遍历全部元素，记录第一个错误位置 `first_err_pos` 与错误总数 `err`；`check_level >= 2`（即 `-c 2`）时才打印首错详情（[L53-L55](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_check_common.cc#L53-L55)），任何级别都打印 `total err is N`。整数类型走精确相等比对，如 `check_buf_result_int8`（[L62-L86](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_check_common.cc#L62-L86)）。AlltoAll 系列因为每个 rank 的期望值不同（`check_val = i + 1`），有独立的比对函数族（如 [L374-L396](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_check_buf_init.cc#L374-L396) 按 `recv_disp` 分段校验）。

**（4）AllReduce 测试如何串起这三步**：见 [hccl_allreduce_rootinfo_test.cc:L60-L109](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc#L60-L109)——`init_buf_val()` 生成期望 buffer，`check_buf_result()` 把 Device 输出拷回 Host 后按 dtype 调用对应的 `check_buf_result_*`，失败则 `check_err++`。基类中 `val` 固定为 2（[hccl_opbase_rootinfo_base.h:L66](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.h#L66)）。

**（5）溢出关校验**：AllReduce 自己的 `is_data_overflow()` 在 [hccl_allreduce_rootinfo_test.cc:L178-L214](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc#L178-L214)，按 dtype 与 rank_size 阈值调用 `no_verification()`；基类版本的阈值常量 `RANKSIZE_TH_FP16=16`、`RANKSIZE_TH_INT8=7` 等定义在 [hccl_opbase_rootinfo_base.h:L27-L31](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.h#L27-L31)，基类通用判定在 [hccl_opbase_rootinfo_base.cc:L135-L164](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc#L135-L164)。这些阈值正是文档 [execution.md:L256-L328](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/execution.md#L256-L328) 那张「最大卡数」表格的源码依据（例如 Prod+fp16 阈值 16、Prod+int8 阈值 7 对应表中 15/6 档——表给的是「仍可校验的最大卡数」，阈值常量是「必关的最小卡数」，相差 1）。

#### 4.3.4 代码实践

1. **实践目标**：亲手推演一次校验的期望值。
2. **操作步骤**：假设 2 卡 fp32 Sum AllReduce，`val=2`。写一个 10 行的 Python 片段（示例代码，非项目代码）模拟 `hccl_reduce_check_buf_init` 与 `check_buf_result_float`：

   ```python
   count, val, rank_size = 8, 2, 2          # 模拟 data->count=8
   host_buf = [float(val)] * count          # hccl_host_buf_init
   check_buf = [val * rank_size] * count    # SUM 分支期望值
   result = host_buf[:]                     # 假设通信正确，输出=期望
   err = sum(1 for c, r in zip(check_buf, result) if abs(c - r) > 1e-6)
   print("total err is", err)               # 预期 0 → success
   ```

3. **需要观察的现象**：把 `result` 中任意一个元素改成 3.0，`err` 变为 1——对应源码里 `check_err++` 与 failed 输出。
4. **预期结果**：理解「期望值在 Host 侧生成、比对也在 Host 侧完成、Device 只负责算」的分工。

#### 4.3.5 小练习与答案

**练习 1**：为什么浮点比对不用精确相等，整数却可以？
**答案**：归约运算在浮点上有舍入误差（尤其 fp16/bf16），逐位相等会大量误报；所以 `check_buf_result_float` 用相对误差阈值（`HCCL_EPSION_FLOAT * 100`）判定。整数的 Sum/Max/Min 结果是精确的，直接比较即可（[hccl_check_common.cc:L62-L86](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_check_common.cc#L62-L86)）。

**练习 2**：`-c 2` 相比 `-c 1` 多输出什么？
**答案**：多打印第一个错误元素的期望值与实际值（`exp`/`act` 行），方便定位是哪个位置的数据开始出错；`-c 1` 只打印 `total err is N`。

**练习 3**：8 卡 int8 Sum AllReduce 会不会自动关校验？
**答案**：会。基类 `is_data_overflow()` 的 SUM 分支规定 int8 且 `rank_size >= RANKSIZE_TH_INT64(63)` 才关，但 AllReduce 覆写版还用 `ALLREDUCE_SUM_RESULE_OVERFLOW` 宏按 int8 精度判定（int8 约 7 位有效精度，上限 63 卡），8 卡未超，不关校验；输出应为 success。若换成 64 卡则超出 int8 Sum 的可精确范围，check_result 显示 NULL。（待本地验证）

### 4.4 性能统计：event 计时与带宽计算

#### 4.4.1 概念说明

输出表格有四个统计量：`data_size`、`aveg_time(us)`（文档里写作 avg_time，源码头字符串是 `aveg_time`）、`alg_bandwidth(GB/s)`、`check_result`。它们的计算分布在两个文件：

- **计时与统计量计算**：`HcclOpBaseAllreduceTest::cal_execution_time()`（各算子测试类各自实现，本讲以 allreduce 为例）。
- **表格打印**：中间层基类 `HcclOpBaseTest::print_execution_time()` 统一负责。

带宽公式的关键是一个换算常量：

\[ \text{alg\_bandwidth} = \frac{\text{malloc\_kSize (Bytes)}}{\text{average\_time (us)}} \times \frac{10^6}{10^9} \]

`B_US_TO_GB_S = 1.0E6 / 1.0E9` 把「字节/微秒」换算成「GB/s」（1e6 微秒 = 1 秒，1e9 字节 = 1 GB，GB 按 10 进制 GB 算）。注意 `malloc_kSize` 对 AllReduce 而言就是单卡数据量（`count × type_size`），所以这是**算法带宽**：单卡数据量 ÷ 耗时，没有乘 rank_size 系数。

#### 4.4.2 核心流程

每个数据量大小的测试节奏（`hccl_op_base_test()`）：

```text
溢出预判 is_data_overflow
    ↓
Host 灌数 → 拷贝到 Device send_buff
    ↓
warmup_iters 轮 HcclAllReduce        ← 预热，排除首轮建链等干扰，不计入统计
    ↓
aclrtRecordEvent(start_event, stream)
    ↓
iters 轮 HcclAllReduce               ← 计时轮
    ↓
aclrtRecordEvent(end_event, stream)
aclrtSynchronizeStream(stream)       ← 等 stream 排空，event 时间戳已定格
aclrtEventElapsedTime(&time, ...)    ← 毫秒
    ↓
(校验开启时) 重灌输入 + 单跑一次 + check_buf_result
    ↓
cal_execution_time(time):
    total_time_us  = time * 1000               (ms → us)
    average_time_us = total_time_us / iters
    alg_bandwidth   = malloc_kSize / average_time_us * B_US_TO_GB_S
    print_execution_time(...)
```

#### 4.4.3 源码精读

计时主循环在 [hccl_allreduce_rootinfo_test.cc:L132-L176](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc#L132-L176) 的 `hccl_op_base_test()`：L148-L150 预热轮、L152 记 start_event、L154-L156 计时轮、L158-L160 记 end_event 并同步 stream、L162-L163 取毫秒耗时、L165-L172 校验（重灌 + 单跑一次 + 比对）。

统计量计算在 [hccl_allreduce_rootinfo_test.cc:L111-L119](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc#L111-L119) 的 `cal_execution_time()`，这就是 `avg_time` 与 `alg_bandwidth` 两个数字的诞生地。

表格打印在 [hccl_opbase_rootinfo_base.cc:L166-L209](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc#L166-L209) 的 `print_execution_time()`：表头四个字段名是基类成员字符串（[hccl_opbase_rootinfo_base.h:L71-L74](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.h#L71-L74)）；`check == 0` 时 check_result 一律打 NULL（L177），开启校验时本 rank 失败打 failed 详情、root rank 汇总打 success/failed（L183-L207）。表头那行 `the minbytes is ... iters is ...` 则在 `HcclTest::init_hcclComm()` 里由 root rank 打印（[hccl_test_common.cc:L912-L917](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L912-L917)）。

换算常量 `B_US_TO_GB_S` 与各 dtype 阈值定义在 [hccl_opbase_rootinfo_base.h:L27-L31](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.h#L27-L31)。

dtype → 元素个数的换算 `init_data_count()` 在 [hccl_opbase_rootinfo_base.cc:L46-L99](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc#L46-L99)：`count = (data_size + type_size - 1) / type_size`（向上取整），AllReduce 的 `malloc_kSize = count × type_size`（[hccl_allreduce_rootinfo_test.cc:L121-L124](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc#L121-L124)）。

结果示例与字段解释见文档 [execution.md:L209-L254](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/execution.md#L209-L254)。

#### 4.4.4 代码实践（本讲主实践）

1. **实践目标**：跑通（或纸面推演）一次两卡 AllReduce 带宽测试，把输出表格的每一列反追到源码。
2. **操作步骤**：
   - 有两卡环境时：在 `${INSTALL_DIR}/tools/hccl_test` 下执行

     ```bash
     mpirun -n 2 ./bin/all_reduce_test -p 2 -b 8M -e 64M -f 2 -d fp32 -o sum -c 1
     ```

     无环境则在文档 [execution.md:L219-L236](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/execution.md#L219-L236) 的示例输出上完成后续步骤。
   - 记录输出的四列表格与表头前的 `the minbytes is ...` 行。
   - 在源码中定位每个量的出处，填这张表：

     | 输出量 | 计算位置 |
     | --- | --- |
     | `data_size` 列 | `HcclTest::check_data_count` 对齐后的 `data->data_size`，经 `opbase_test_by_data_size` 循环产出（hccl_test_common.cc:969-998） |
     | `avg_time/aveg_time(us)` | `cal_execution_time` 的 `total_time_us / iters`（hccl_allreduce_rootinfo_test.cc:114） |
     | `alg_bandwidth(GB/s)` | `malloc_kSize / average_time_us * B_US_TO_GB_S`（hccl_allreduce_rootinfo_test.cc:115） |
     | `check_result` | `print_execution_time` 按 `check`/`check_err` 三分支打印（hccl_opbase_rootinfo_base.cc:166-209） |
   - 抽一行数据验算：如示例输出中 64M 行（67108864 Bytes，2630.20 us），带宽 ≈ 67108864 / 2630.20 × 1e6/1e9 ≈ 25.5 GB/s（示例值为 23.76，因示例数据的 data_size 实测耗时略有出入，量级一致即可；以本地实测数据验算应严格吻合）。
3. **需要观察的现象**：数据量每翻倍一档，带宽整体上升（固定开销被摊薄），小数据量档位带宽很低。
4. **预期结果**：能指出 `avg_time` 的除数是 `iters`（默认 20）、预热轮不计入统计、带宽分子是单卡字节数而非全卡总和。本实践的两卡部分为「待本地验证」（纸面部分可直接完成）。

#### 4.4.5 小练习与答案

**练习 1**：为什么预热轮不计入耗时统计？
**答案**：首轮迭代可能包含 socket 建链、内存初始化等一次性开销（文档 [cmdline_options_desc.md:L180-L183](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/hccl_test/cmdline_options_desc.md#L180-L183)）。源码中 start_event 在预热循环之后才记录（hccl_allreduce_rootinfo_test.cc:148-152），从机制上把预热排除在计时窗口外。

**练习 2**：把 `-n 20`（默认）改成 `-n 100`，`avg_time` 会更准吗？
**答案**：样本更多、平均更稳，但注意 `-t 1`（仅统计 Device 耗时）场景下 `-n` 与 `-w` 强制不大于 100（`check_only_device_exec_time`，[hccl_test_common.cc:L397-L421](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L397-L421)）。

**练习 3**：输出里 `check_result` 为 NULL，一定是坏事吗？
**答案**：不一定。NULL 只表示「没有做校验」：要么用户传了 `-c 0`，要么工具判定数值会溢出主动关闭（`no_verification()`），通信本身可能完全正常。failed 才表示校验发现数据不一致。

## 5. 综合实践

**任务：给一次完整的带宽测试写「结果解读报告」。**

1. 选一个测试命令（如 2 卡 `mpirun -n 2 ./bin/all_reduce_test -p 2 -b 8K -e 64M -f 2 -d fp32 -o sum -w 5 -n 20 -c 2`），运行或以文档示例输出为样本。
2. 对输出做四件事，每件事都要能指向一个源码位置：
   - 解释表头 `the minbytes is ...` 行由谁打印（`init_hcclComm` 中 root rank 分支）。
   - 任取一行，用 `cal_execution_time` 的公式手工验算 `alg_bandwidth`。
   - 说明该行 `check_result` 取值的判定链：`-c` 值 → `is_data_overflow` 是否关校验 → `check_buf_result` 的 `check_err` → `print_execution_time` 三分支。
   - 对比 8K 与 64M 两行的带宽，用「固定开销摊薄」解释为什么小数据量带宽低。
3. 附加题：把 `-d fp32` 换成 `-d int8`、`-o sum`，卡数提到 64（或纸面推演），预测 `check_result` 列会变 NULL 并给出源码依据（`ALLREDUCE_SUM_RESULE_OVERFLOW` 宏与 int8 精度阈值）。

完成这份报告后，你对 hccl_test 的掌握就从「会敲命令」进入了「知道每个数字从哪来」的层级。

## 6. 本讲小结

- hccl_test 的运行 = mpirun 编排 + 测试二进制 + 环境变量三层组合；工具辅助变量 `HCCL_TEST_USE_DEVS`/`HCCL_TEST_PROFILING` 由 `HcclTest::get_env_resource` 与 `get_mpi_proc` 消费。
- 用户输入的 `-b/-e/-i` 会被 `check_data_count` 做 rank_size 倍数对齐（小块对齐 rank_size、大块对齐 rank_size×512、步长向上取整），这解释了输出 data_size 与输入的微小差异。
- 正确性校验是「固定值灌数 → 数学期望 → 逐元素比对」三段式：灌数与期望值在 `hccl_check_buf_init.cc`（函数表按 dtype 分发），比对在 `hccl_check_common.cc`（浮点用相对误差、整数精确相等，`-c 2` 打印首错详情）。
- 期望值会溢出的场景（按 dtype 精度与卡数阈值）自动 `no_verification()`，check_result 显示 NULL 而非 failed——文档的「最大卡数表」即来源于源码中的 `RANKSIZE_TH_*` 常量与 `ALLREDUCE_SUM_RESULE_OVERFLOW` 宏。
- 性能统计的关键链路：预热轮不计入 → event 计时 → `total_time_us/iters` 得平均耗时 → `malloc_kSize/average_time_us × B_US_TO_GB_S` 得算法带宽（分子是单卡数据量），打印统一收口在 `print_execution_time`。

## 7. 下一步学习建议

hccl_test 单元到此完成。建议：

1. 回顾本单元三讲，画出从 `mpirun` 敲下到表格输出打印的完整时序图（main → HcclTest 生命周期 → rootinfo 建链 → opbase_test_by_data_size 循环 → cal_execution_time/print_execution_time）。
2. 进入 u6 单元：先读 u6-l1 测试体系了解 oam-tools 的 UT/ST 如何覆盖这些组件；如果你的兴趣是给 hccl_test 贡献新算子测试，可直接跳到 u6-l4 二次开发实战，把 u5-l2 的「四覆写点 + Makefile 映射」和本讲的校验/统计链路组合起来实践。
3. 延伸阅读仓库外链的《通信算子接口》文档（hccl 仓），理解每个测试命令背后对应的 HCCL API 支持范围。
