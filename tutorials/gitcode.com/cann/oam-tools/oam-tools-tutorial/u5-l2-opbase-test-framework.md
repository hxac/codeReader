# u5-l2 opbase 测试框架：rootinfo 测试如何组织

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 hccl_test 的三层类继承体系（`HcclTest` → `HcclOpBaseTest` → `HcclOpBaseXxxTest`），并说出每一层负责什么。
2. 理解 rootinfo 机制：为什么建通信域需要"root 进程生成 `HcclRootInfo` + MPI 广播 + 全体 `HcclCommInitRootInfo`"这三步。
3. 读懂一个算子测试类（以 allreduce 为例）必须覆写哪几个虚函数、每个虚函数在测试主流程的哪个时机被调用。
4. 掌握新增一个算子测试的完整步骤：写 `.h`/`.cc`、实现工厂函数 `init_opbase_ptr`、在 Makefile 注册编译目标。

本讲承接 u5-l1（hccl_test 总览：Makefile 双入口、11 个二进制共享一份 main）。上一讲看的是"整个工程怎么转起来"，本讲往下钻一层，看"一个算子测试类的骨架长什么样、怎么复用"。

## 2. 前置知识

### 2.1 集合通信与 rank

集合通信（Collective Communication）是多个设备（NPU）共同参与的数据交换操作，如 AllReduce（所有卡的数据归约后每张卡拿到相同结果）、Broadcast（一张卡的数据广播给所有卡）。参与通信的每个进程/设备称为一个 **rank**，用 `rank_id`（自己是几号）和 `rank_size`（总共几个）描述。

### 2.2 rootinfo 是什么

HCCL 建立通信域（`HcclComm`）前，所有 rank 必须对齐一份"建链信息"——包含通信使用的 IP、端口、rank 划分等。这份信息由 HCCL 提供 `HcclGetRootInfo` 在任意一个进程（称为 **root 进程**，默认 rank 0）上生成，其余进程拿到同一份字节串后各自调用 `HcclCommInitRootInfo` 完成建链。这份字节串就叫 **rootinfo**（`HcclRootInfo` 结构，固定 `HCCL_ROOT_INFO_BYTES` 字节）。

hccl_test 用 MPI 把 rootinfo 从 root 进程广播（`MPI_Ibcast`）到所有进程——这也是为什么 hccl_test 必须用 `mpirun` 拉起：MPI 负责"进程编排 + rootinfo 分发"，HCCL 负责"通信域建立 + 集合通信执行"。

### 2.3 模板方法模式

本讲反复出现的代码组织手法是**模板方法模式**：基类写好"固定流程"（什么时候建链、什么时候分配内存、循环哪些数据量），把"每算子不同的部分"声明为虚函数留给子类覆写（初始化校验缓冲、执行 HCCL 调用、校验结果）。C++ 里基类指针调用虚函数时，实际执行的是子类的实现。

### 2.4 ACL 运行时与错误检查宏

hccl_test 通过 AscendCL（ACL）管理设备资源（`aclrtMalloc`/`aclrtFree`、stream、event）。代码里两个高频宏（定义在 `hccl_test_common.h`）：

- `ACLCHECK(cmd)`：执行 ACL 调用，失败则打印文件行号并 `return` 错误码；
- `HCCLCHECK(cmd)`：同理，包 HCCL 调用。

它们让"每步都检查返回值"不至于写成满屏 if-else。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/hccl_test/common/src/hccl_test_common.h](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.h) | `HcclTest` 基类声明：命令行解析、参数校验、MPI/rootinfo 建链、数据量循环、收发内存管理；另有 `ACLCHECK`/`HCCLCHECK` 宏与 `DataSize` 结构 |
| [src/hccl_test/common/src/hccl_test_common.cc](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc) | `HcclTest` 全部实现，本讲重点读 `init_hcclComm`、`opbase_test_by_data_size`、`get_buff_size` |
| [src/hccl_test/common/src/hccl_opbase_rootinfo_base.h](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.h) | `HcclOpBaseTest` 中间层声明：dtype→count 换算、溢出判定、耗时打印；定义 `HcclDataTypePrecision` 精度枚举 |
| [src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc) | `HcclOpBaseTest` 实现：`init_data_count` 大 switch、`is_data_overflow`、`print_execution_time` |
| [src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.h](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.h) | allreduce 测试类声明 + SUM 溢出判定宏 |
| [src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc) | allreduce 测试类实现 + 工厂函数 `init_opbase_ptr`（本讲范本） |
| [src/hccl_test/opbase_test/hccl_reduce_rootinfo_test.cc](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_reduce_rootinfo_test.cc) | reduce 测试实现——"带 root 参数算子"的现成参照 |
| [src/hccl_test/common/src/hccl_test_main.cc](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_main.cc) | 11 个二进制共享的 main（u5-l1 已读，本讲回顾工厂调用点） |
| [src/hccl_test/Makefile](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile) | 每个算子目标 ↔ 源文件名的映射表，新增测试的最后一步注册点 |

> 注意：本工程的头文件与源码同放在 `common/src/` 目录下（`.h` 不在单独的 `inc/` 目录），初读时不要按常规目录约定去找。

## 4. 核心概念与源码讲解

### 4.1 三层类体系与 rootinfo 建链

#### 4.1.1 概念说明

hccl_test 的 11 个算子测试共享同一套骨架，靠三层继承组织：

```
HcclTest                      （公共底盘：参数解析、MPI、rootinfo 建链、数据量循环、内存管理）
  └── HcclOpBaseTest          （算子测试中间层：dtype→count 换算、溢出判定、耗时打印、host 缓冲释放）
        └── HcclOpBaseAllreduceTest / HcclOpBaseReduceTest / ...   （每算子一个：真正的 HCCL 调用与结果校验）
```

`HcclTest` 是纯底盘：它自己不知道要测哪个算子（`hccl_op_base_test()` 默认返回 0），只负责把所有算子都需要的"外部环境"准备好。

#### 4.1.2 核心流程

从 main 到通信域建立的关键路径（承接 u5-l1 的 main 流程）：

```
main()
 ├─ init_opbase_ptr()          ← 工厂函数（每个算子的 .cc 里各有一份，链接期决定实例化谁）
 ├─ parse_cmd_line / check_cmd_line
 ├─ get_mpi_proc()             ← MPI_Comm_rank/size → rank_id / rank_size；dev_id = proc_rank % npus
 ├─ device_init()              ← aclInit、SetDevice、建 stream/event
 └─ start_test()
      └─ init_hcclComm()       ← rootinfo 三步曲（见 4.1.3）
         └─ opbase_test_by_data_size()   ← 按数据量循环，进入 4.3 的算子骨架
```

rootinfo 建链三步曲：

1. root 进程调用 `HcclGetRootInfo(&comm_id)` 生成建链信息；
2. 用 `MPI_Ibcast` 把 `comm_id` 的 `HCCL_ROOT_INFO_BYTES` 字节广播给所有进程（失败时广播字符串 `"invalid"` 通知大家退出）；
3. 所有进程拿着同一份 `comm_id` 调用 `HcclCommInitRootInfo(rank_size, &comm_id, rank_id, &hccl_comm)` 建立通信域。

#### 4.1.3 源码精读

类声明与虚函数骨架：[src/hccl_test/common/src/hccl_test_common.h:L118-L166](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.h#L118-L166)——`HcclTest` 把 `hccl_op_base_test()`、`init_data_count()`、`init_malloc_Ksize_by_data()`、`init_send_recv_size_by_data()` 都声明为 virtual，protected 的两个 `init_malloc_Ksize_by_data`/`init_send_recv_size_by_data` 默认实现返回 0，即"基类不假设你需要多少收发内存"。

rootinfo 三步曲：[src/hccl_test/common/src/hccl_test_common.cc:L906-L967](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L906-L967)。关键片段：

```cpp
// root 进程：生成 rootinfo，失败则广播 "invalid" 让全体退出
if (rank_id == root_rank) {
    HcclResult getRootInfo = HcclGetRootInfo(&comm_id);
    if (getRootInfo == HCCL_SUCCESS) {
        MPI_Ibcast(&comm_id, HCCL_ROOT_INFO_BYTES, MPI_CHAR, root_rank, MPI_COMM_WORLD, &request);
    } else { ... MPI_Ibcast(send_str /* "invalid" */, ...); }
    MPI_Wait(&request, &status);
} else {
    // 非 root 进程接收，检测到 "invalid" 则报错返回
    MPI_Ibcast(&comm_id, HCCL_ROOT_INFO_BYTES, MPI_CHAR, root_rank, MPI_COMM_WORLD, &request);
    MPI_Wait(&request, &status);
}
// 全体进程：用同一份 rootinfo 建立通信域
HCCLCHECK(HcclCommInitRootInfo(rank_size, &comm_id, rank_id, &hccl_comm));
```

同文件 [src/hccl_test/common/src/hccl_test_common.cc:L886-L904](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L886-L904) 的 `init_hcclComm_without_nslb` 是实际建链入口：配置了加速器模式或对称内存时走 `HcclCommInitRootInfoConfig`（带 `HcclCommConfig`），否则走无配置版本。

rank 与设备的映射：[src/hccl_test/common/src/hccl_test_common.cc:L705-L740](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L705-L740)——`get_mpi_proc` 用 `MPI_Comm_rank/size` 填 `rank_id/rank_size`，再按 `dev_id = proc_rank % npus` 把本进程绑到本机某张 NPU（也支持 `HCCL_TEST_USE_DEVS` 环境变量指定可用卡列表）。

main 中的工厂调用点：[src/hccl_test/common/src/hccl_test_main.cc:L37-L43](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_main.cc#L37-L43)——main 只认识基类指针 `HcclTest*`，具体new哪个子类由每个算子 `.cc` 里的 `init_opbase_ptr` 决定。

数据量循环：[src/hccl_test/common/src/hccl_test_common.cc:L969-L998](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_test_common.cc#L969-L998)——`opbase_test_by_data_size` 按 `min_bytes → max_bytes`（步进 `step_bytes` 或倍增 `step_factor`）循环，每个数据量：`get_buff_size` 问子类要内存大小 → 分配 send/recv 显存 → 调 `hccl_op_base_test()`（虚函数，进入算子骨架）→ 释放。这就是"一条命令扫一遍 64MB~1GB 带宽曲线"的实现处。

#### 4.1.4 代码实践

1. **实践目标**：亲眼验证 rootinfo 广播是所有 rank 共同参与的。
2. **操作步骤**：
   - 阅读 `init_hcclComm`（行号见上），确认 root 与非 root 两个分支的代码路径；
   - 在纸面画出 4 个 rank（rank 0 为 root）时 `MPI_Ibcast` 的数据流向图。
3. **需要观察的现象**：图中只有 rank 0 调用 `HcclGetRootInfo`，4 个 rank 都调用 `MPI_Ibcast`/`MPI_Wait`，最后 4 个 rank 都调用 `HcclCommInitRootInfo`。
4. **预期结果**：能回答"为什么 `HcclGetRootInfo` 只在一个进程调用，而 `HcclCommInitRootInfo` 每个进程都要调用"——前者生成建链信息（一份即可），后者在各进程本地建立通信域（各自一份 `hccl_comm` 句柄）。真实多卡运行验证需昇腾环境，本实践为源码阅读型，**待本地验证**（若在真机跑，可故意在非 root 进程打印收到的 `comm_id` 前 8 字节，应与 root 进程一致）。

#### 4.1.5 小练习与答案

**练习 1**：如果 root 进程 `HcclGetRootInfo` 失败，其余进程如何得知并退出？
**答案**：root 进程用 `MPI_Ibcast` 广播字符串 `"invalid"` 代替 rootinfo；非 root 进程接收后用 `strcmp(&comm_id, "invalid")` 检测到无效数据，打印错误并返回 -1（见 `hccl_test_common.cc` L922-L940）。

**练习 2**：`root_rank` 这个成员除了用于 rootinfo 广播，还在哪里生效？
**答案**：两处。一是命令行校验（`check_cmd_line` 要求 `0 <= root_rank < rank_size`，`hccl_test_common.cc` L456-L462）；二是被算子测试子类使用——带 root 参数的算子（如 Reduce/Broadcast）把 `root_rank` 直接传给 HCCL 调用，且只有 root rank 校验计算结果（见 4.3 的 reduce 对照）。

### 4.2 HcclOpBaseTest 中间层：换算、溢出判定与结果打印

#### 4.2.1 概念说明

`HcclOpBaseTest` 夹在底盘和具体算子之间，收拢"所有算子测试都要做、但与算子语义无关"的三件事：

1. **dtype → count/type_size 换算**：命令行给的是字节数（`-b 64M`），HCCL 接口要的是元素个数（`count`）；
2. **数值溢出判定**：卡数（rank_size）一多，SUM/PROD 的期望结果会超出数据类型精度范围，此时正确性校验没有意义，框架自动关闭校验；
3. **统一结果打印**：所有算子输出同一张表（data_size / 平均耗时 / 算法带宽 / 校验结果）。

#### 4.2.2 核心流程

溢出判定的数学直觉：设数据类型有效精度为 \(p\) 位（如 int8 为 7，fp16 尾数为 10），val 初值为 2，SUM 的期望结果约为 \(val \times rank\_size = 2 \times rank\_size\)。当

\[ 2 \times rank\_size > 2^{p} \quad\Longleftrightarrow\quad rank\_size > 2^{p-1} \]

时期望值不可精确表示，校验会误报，于是调用 `no_verification()` 把 `check` 置 0。代码里用宏 `ALLREDUCE_SUM_MAX_RANKSIZE(dtype)`（即 \( (2^{p}-1)/2 \)）表达这个阈值。

算法带宽的计算：

\[ \text{algorithm\_bandwidth(GB/s)} = \frac{\text{malloc\_kSize (Bytes)}}{\text{average\_time (us)}} \times \frac{10^6}{10^9} \]

其中 \(10^6/10^9 = 10^{-3}\) 即常量 `B_US_TO_GB_S`（Bytes/微秒 换算到 GB/秒）。

#### 4.2.3 源码精读

中间层声明与精度枚举：[src/hccl_test/common/src/hccl_opbase_rootinfo_base.h:L27-L75](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.h#L27-L75)——定义 `RANKSIZE_TH_*` 阈值常量（如 `RANKSIZE_TH_INT8 = 7`）、`HcclDataTypePrecision` 精度枚举（int8 精度 7、fp16 精度 10 等）和拼接宏 `CONCAT`，供子类的溢出宏使用。

dtype 大 switch：[src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc:L46-L99](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc#L46-L99)——`init_data_count` 按 `dtype` 把 `data->data_size`（字节）换算成 `data->count`（元素数，向上取整 `(size + sizeof(T) - 1)/sizeof(T)`）并记录 `type_size`。fp8 系列、uint16/32 等新类型也在这份 switch 里，这是"dtype 支持矩阵"的唯一登记处。

基类版溢出判定：[src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc:L135-L164](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc#L135-L164)——`is_data_overflow` 对 PROD/SUM 按 dtype 查阈值表调 `no_verification()`。注意它是 virtual：allreduce 子类覆写了更精确的版本（见 4.3.3）。

`no_verification`：[src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc:L111-L119](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc#L111-L119)——把 `check` 置 0，root rank 打一条 Warning 后关闭 dump 打印。

统一结果表打印：[src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc:L166-L209](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc#L166-L209)——`print_execution_time` 分两支：`check == 0` 时校验列打 `NULL`；开校验时本地 `check_err != 0` 的 rank 打 failed 行，root rank 汇总打 success/failed 行。表头（`print_header`）只打一次，只有 root rank 输出。

host 缓冲释放：[src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc:L211-L226](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc#L211-L226)——析构函数调 `destory_alloc_buf` 释放子类在 host 侧申请的三块内存（`host_buf`/`recv_buff_temp`/`check_buf`），保证"谁申请、框架统一释放"。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证 dtype→count 换算与溢出阈值。
2. **操作步骤**：
   - 写一个 10 行的独立小程序（示例代码，不修改仓库），把 `init_data_count` 的换算公式抄出来：

     ```cpp
     // 示例代码：验证 count 向上取整换算
     size_t data_size = 100;           // 100 字节
     size_t ts = sizeof(short);        // fp16/int16 → 2 字节
     size_t count = (data_size + ts - 1) / ts;
     printf("count = %zu\n");          // 期望 50
     ```

   - 再按公式 \( (2^{p}-1)/2 \) 手算 int8（p=7）的 SUM 最大安全 rank_size，与 `hccl_reduce_rootinfo_test.cc` 中 `REDUCE_SUM_RESULE_OVERFLOW` 宏的判定结果对照。
3. **需要观察的现象**：100 字节 / 2 字节 = 50 个元素；int8 时 \((2^7-1)/2 = 63\)（整型除法向下取整），即 rank_size ≤ 63 才校验。
4. **预期结果**：手算结果与源码宏展开一致。编译运行只需 g++，无设备依赖。**待本地验证**（公式推导可直接核对源码）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RANKSIZE_TH_INT8 = 7` 而 fp32 是 128？
**答案**：PROD 操作期望结果为 \(val^{rank\_size} = 2^{rank\_size}\)，int8 只有 7 位精度，\(2^7\) 即溢出，所以阈值是 7；fp32 尾数 23 位，阈值为 \(2^7=128\) 是工程上对累乘/累加稳定性的经验阈值，两者在 `hccl_opbase_rootinfo_base.h` L28-L32 定义。

**练习 2**：`check_err` 与 `check` 两个变量的区别是什么？
**答案**：`check` 是用户命令行参数 `-c`（0 不校验/1 静默/2 详细），控制"要不要校验"；`check_err` 是运行时计数器，`check_buf_result` 比对失败时 `check_err++`，`print_execution_time` 根据它决定打印 success 还是 failed。

### 4.3 HcclOpBaseAllreduceTest：一个算子测试类的完整骨架

#### 4.3.1 概念说明

最底层每算子一个类，命名规律 `HcclOpBase<算子名>Test`。它要做的只有四件事（其余全部继承）：

| 覆写/实现 | 作用 | 调用时机 |
| --- | --- | --- |
| `init_malloc_Ksize_by_data()` | 告诉框架本轮数据量需要多大收发内存 | 每个数据量循环开始（`get_buff_size`） |
| `init_send_recv_size_by_data()` | 给出 send/recv 各自字节数 | 同上 |
| `hccl_op_base_test()` | **测试主体**：初始化数据 → 预热 → 计时执行 → 校验 → 打印 | `opbase_test_by_data_size` 循环体 |
| `init_buf_val()` / `check_buf_result()` | 准备期望值缓冲 / 逐元素比对 | 由 `hccl_op_base_test` 内部调用 |
| `init_opbase_ptr()` 工厂函数 | `new` 出本类并返回基类指针 | main 启动时（每二进制一份） |

#### 4.3.2 核心流程

`hccl_op_base_test()`（测试主体）的固定节奏：

```
is_data_overflow()                       ← 先判定本组参数下校验是否可行
申请 host_buf → hccl_host_buf_init 填初值 → 拷贝到 send_buff（显存）
check>=1 ? init_buf_val()                ← 准备期望值缓冲
start_profile_device_time_if_needed()    ← -t 1 时的纯 device 计时准备
for warmup_iters: HcclAllReduce(...)     ← 预热
record start_event
for iters:        HcclAllReduce(...)     ← 正式计时轮
record end_event → SynchronizeStream → EventElapsedTime
check>=1 ? 重灌输入 → 再跑一次 → check_buf_result()   ← 校验（计时轮数据已被复写）
cal_execution_time() → print_execution_time()
```

计时不校验、校验不复用计时数据——校验前重新 `aclrtMemcpy` host 数据到 `send_buff` 再执行一次通信，保证比对的数据是确定初值算出来的。

#### 4.3.3 源码精读

类声明与覆写清单：[src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.h:L26-L42](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.h#L26-L42)——`HcclOpBaseAllreduceTest: public HcclOpBaseTest`，覆写 `hccl_op_base_test`、`is_data_overflow`、`init_malloc_Ksize_by_data`、`init_send_recv_size_by_data`，私有实现 `init_buf_val`、`check_buf_result`、`cal_execution_time`。文件头部 L24-L25 的两个宏用 `HcclDataTypePrecision` 枚举 + `CONCAT` 拼接出 \( (2^p-1)/2 \) 阈值。

工厂函数：[src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc:L31-L43](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc#L31-L43)——`init_opbase_ptr` 只 `new HcclOpBaseAllreduceTest()` 并返回，`delete_opbase_ptr` 负责 delete。11 个二进制共享同一份 main，靠的就是"每份 opbase 源文件各带一份同名工厂函数、链接期由 Makefile 选用"。

测试主体：[src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc:L132-L176](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc#L132-L176)——即 4.3.2 伪代码的落地。三次 `HcclAllReduce` 调用分别出现在预热轮（L148-L150）、计时轮（L154-L156）和校验轮（L165-L172），三处参数完全相同：

```cpp
HCCLCHECK(HcclAllReduce((void *)send_buff, (void*)recv_buff, data->count,
                        (HcclDataType)dtype, (HcclReduceOp)op_type, hccl_comm, stream));
```

带宽计算：[src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc:L111-L119](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc#L111-L119)——`cal_execution_time`：`average_time_us = time(ms)*1000/iters`，`bandwidth = malloc_kSize / average_time_us * B_US_TO_GB_S`。注意这是**算法带宽**（搬运数据量/耗时），不是总线带宽。

内存尺寸与收发字节数：[src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc:L121-L130](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc#L121-L130)——allreduce 收发等长（都等于 `malloc_kSize = count*type_size`）；对比 allgather 这类算子 recv 是 send 的 rank_size 倍——差异就通过这两个覆写函数表达，框架其余部分零改动。

期望值准备与逐类型比对：[src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc:L60-L109](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc#L60-L109)——`init_buf_val` 调 `hccl_reduce_check_buf_init` 生成期望缓冲；`check_buf_result` 把 `recv_buff` 拷回 host 后按 dtype 分发到 `check_buf_result_float/int8/int32/half/int64`（这些比对函数来自 `common/src/hccl_check_common.h`，u5-l3 精读），失败则 `check_err++`。

**带 root 参数的对照**——reduce 测试：[src/hccl_test/opbase_test/hccl_reduce_rootinfo_test.cc:L153](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_reduce_rootinfo_test.cc#L153) 的 HCCL 调用多传一个 `root_rank`：

```cpp
HCCLCHECK(HcclReduce((void *)send_buff, (void*)recv_buff, data->count, (HcclDataType)dtype,
                     (HcclReduceOp)op_type, root_rank, hccl_comm, stream));
```

配套两处差异：期望值只在 root rank 生成（`init_buf_val` 内 `if(rank_id == root_rank)`，L63-L65）；校验也只在 root rank 进行（`check_buf_result` 开头 `if(rank_id != root_rank) return 0;`，L75-L77）——因为 Reduce 只有 root 拿到完整结果。这就是"带 root 变体"相对 allreduce 的全部改动点。

allreduce 版溢出判定：[src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc:L178-L214](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc#L178-L214)——覆写基类版本，SUM 分支改用头文件里的 `ALLREDUCE_SUM_RESULE_OVERFLOW` 精度宏（基于 \( (2^p-1)/2 \)），比基类的粗粒度阈值表更精确。

Makefile 注册：[src/hccl_test/Makefile:L37](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L37) 与 [L56-L66](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L56-L66)、[L79-L82](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L79-L82)——`LIST` 列出 11 个目标名，每个目标用 `目标名: SRC = 源文件名` 指定唯一一份 opbase 源文件，与全部 `common/src/*.cc` 一起编出一个二进制。新增算子测试 = 在 LIST 加目标名 + 加一行 SRC 映射。

#### 4.3.4 代码实践

见第 5 节综合实践（本讲的主实践即"写一个新算子测试骨架"，放在综合实践统一完成）。

#### 4.3.5 小练习与答案

**练习 1**：为什么计时轮结束后校验前要重新把 `host_buf` 拷到 `send_buff` 再跑一次通信？
**答案**：计时轮跑了 `iters` 次 allreduce，`send_buff`/`recv_buff` 内容已不可控（且输入可能被框架/in-place 语义改写）；校验需要"确定初值 → 唯一期望结果"，所以重灌输入、单跑一次、再比对。

**练习 2**：如果把 allreduce 测试改成 allgather 测试，`init_send_recv_size_by_data` 应该怎么写？
**答案**：allgather 每个 rank 收到所有 rank 拼接的数据，所以 `send_bytes = malloc_kSize; recv_bytes = malloc_kSize * rank_size;`。可对照仓库中 [src/hccl_test/opbase_test/hccl_allgather_rootinfo_test.h](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allgather_rootinfo_test.h) 验证你的答案。

**练习 3**：`start_profile_device_time_if_needed(16*1024*1024)` 的参数 16MB 是什么意思？
**答案**：这是 `-t`（仅统计 device 执行时间）模式的保护阈值：在 950 芯片 + CCU_SCHED 加速器模式下，数据量 ≥16MB 时继续开 `-t` 可能卡死，框架自动让 `-t` 失效（见 `hccl_test_common.cc` L1260-L1281 的注释与实现）。

## 5. 综合实践

**任务：为一个尚未覆盖的"带 root 参数"集合通信变体编写完整测试骨架。**

当前 11 个已覆盖算子里带 root 的有 Reduce、Broadcast、Scatter；假设你要新增一个假想的 `gather_test`（Gather：所有 rank 的数据收集到 root，HCCL 是否提供 `HcclGather` 接口以你机器上 `${ASCEND_DIR}/include/hccl/hccl.h` 为准——**待确认**），照抄 reduce 模板完成三份改动。以下均为**示例代码**（不修改仓库，写在草稿区即可）。

**第一步：`opbase_test/hccl_gather_rootinfo_test.h`（对照 `hccl_reduce_rootinfo_test.h`）**

```cpp
// 示例代码：gather 测试类骨架
#ifndef __HCCL_GATHER_ROOTINFO_TEST_H_
#define __HCCL_GATHER_ROOTINFO_TEST_H_
#include "hccl_test_common.h"
#include "hccl_check_common.h"
#include "hccl_opbase_rootinfo_base.h"
namespace hccl {
class HcclOpBaseGatherTest : public HcclOpBaseTest
{
public:
    HcclOpBaseGatherTest();
    virtual ~HcclOpBaseGatherTest();
    virtual int hccl_op_base_test();      // 测试主体
protected:
    size_t init_malloc_Ksize_by_data() override;                        // count*type_size
    void init_send_recv_size_by_data(size_t &send_bytes, size_t &recv_bytes) override;
private:
    virtual int init_buf_val();           // 仅 root rank 生成期望值
    virtual int check_buf_result();       // 仅 root rank 比对
    void cal_execution_time(float time);
};
}
#endif
```

**第二步：`opbase_test/hccl_gather_rootinfo_test.cc` 关键差异点（对照 `hccl_reduce_rootinfo_test.cc`）**

```cpp
// 示例代码：工厂函数——每个算子二进制的"身份"
HcclTest* hccl::init_opbase_ptr(HcclTest* opbase)
{
    opbase = new HcclOpBaseGatherTest();
    return opbase;
}

// 差异 1：收发不等长——send 是单 rank 数据，root 收全量，非 root 可以不分配 recv
void HcclOpBaseGatherTest::init_send_recv_size_by_data(size_t &send_bytes, size_t &recv_bytes)
{
    send_bytes = malloc_kSize;
    recv_bytes = (rank_id == root_rank) ? malloc_kSize * rank_size : 0;  // 具体以 HCCL 接口语义为准
}

// 差异 2：HCCL 调用换成 Gather 语义（接口名/参数以 hccl.h 为准，待确认）
//     HCCLCHECK(HcclGather(send_buff, recv_buff, data->count,
//                          (HcclDataType)dtype, root_rank, hccl_comm, stream));

// 差异 3：期望值生成与结果校验都套 if(rank_id == root_rank)，非 root 直接 return 0
```

`hccl_op_base_test()` 的预热/计时/校验三段式、`cal_execution_time`、`is_data_overflow`（Gather 无归约，可不覆写用基类版）均可逐行照抄 reduce。

**第三步：`Makefile` 注册**

```make
# 示例代码：LIST 追加 gather_test，并加一行 SRC 映射
LIST = ... gather_test
gather_test: SRC = hccl_gather_rootinfo_test.cc
```

**验证方式**：
1. 有昇腾 + MPI 环境时：`make ASCEND_DIR=... MPI_HOME=... gather_test`，然后 `mpirun -np 2 ./bin/gather_test -b 1M -e 1M -c 1 -r 0`，观察输出表是否与 reduce 一致（data_size/耗时/带宽/校验列）。**待本地验证**。
2. 无设备时：纸面走查三份 diff，确认除工厂函数、HCCL 调用、root 条件判断、收发字节数四处外，没有任何其他改动——这正好验证"框架复用度高、新增成本低"的设计结论。

## 6. 本讲小结

- hccl_test 用三层类体系组织 11 个算子测试：`HcclTest`（底盘：参数/MPI/建链/数据量循环/内存）、`HcclOpBaseTest`（中间层：dtype→count 换算、溢出判定、统一打印）、`HcclOpBaseXxxTest`（每算子：HCCL 调用与校验），是典型的模板方法模式。
- rootinfo 建链三步曲：root 进程 `HcclGetRootInfo` 生成 → `MPI_Ibcast` 广播（失败广播 `"invalid"` 让全体退出）→ 全体 `HcclCommInitRootInfo` 建通信域；rootinfo 分发是 hccl_test 依赖 MPI 的根本原因。
- 算子测试主体固定节奏"溢出判定 → 灌数 → 预热 → event 计时轮 → 重灌单跑校验 → 算带宽打印"，计时与校验分离保证比对数据确定。
- 每算子差异收敛在四个覆写点：`init_malloc_Ksize_by_data`、`init_send_recv_size_by_data`、`hccl_op_base_test`、`init_buf_val`/`check_buf_result`；带 root 的算子额外差异是 HCCL 调用多传 `root_rank` 且只在 root rank 生成期望值、校验结果。
- 新增算子测试的完整改动集只有三处：一对 `.h/.cc`（含工厂函数 `init_opbase_ptr`）、Makefile 的 `LIST` 与 `SRC` 映射各一行。

## 7. 下一步学习建议

下一讲 u5-l3《hccl_test 运行与结果解读》将深入本讲反复引用的两块"暗箱"：`hccl_check_buf_init`（期望值如何按 op/dtype/val/rank_size 算出）与 `hccl_check_common`（`check_buf_result_float` 等逐元素比对函数与 `-c 0/1/2` 三级校验输出），并结合 `docs/zh/hccl_test/execution.md` 解读一次真实带宽测试的输出。建议先自行浏览 `src/hccl_test/common/src/hccl_check_buf_init.cc` 与 `hccl_check_common.cc`，带着"校验缓冲的期望值公式是什么"的问题进入下一讲。
