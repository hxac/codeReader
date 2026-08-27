# u5-l2 四个代码生成器：内核、主机、连接与 Makefile 的模板化生成

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐段读懂 `kernelcode_gen.py`，说出它如何用字符串拼接按「端口数 × 访问类型」生成 `krnl_ubench.cpp` 与 `krnl_config.h`。
2. 读懂 `hostcode_gen.py` 如何把 `CONSECUTIVE_DATA_SIZE` 范围换算成 payload 循环边界、把 `BANK_FLAG` 注入 `cl_mem_ext_ptr_t`，以及 `connectivity_gen.py` 如何生成 `ubench.ini` 的 `slr/sp/nk` 三条指令。
3. 读懂 `makefile_gen.py`，特别是 `KERNEL_FREQ` 唯一的落点 `--kernel_frequency`，以及生成版与手写版 Makefile 的 `COMMON_REPO` 深度差异。
4. 识别这套脚本里的 Python 2 语法与语义陷阱（裸 `print`、整数除法），独立完成向 Python 3 的迁移。

## 2. 前置知识

**参数空间与单源生成（u5-l1）**。`config.py` 用七个配置项描述微基准参数空间；`generate_microbenchmarks.py` 用六层嵌套循环为每个参数组合生成一个完整工程目录。本讲打开这四个被主脚本调用的生成器黑盒。回忆五个影响带宽的因素与它们的落点：

| 因素 | config.py 维度 | 最终落在哪个生成文件 |
|---|---|---|
| 内核频率 | `KERNEL_FREQ` | Makefile 的 `--kernel_frequency` |
| 并发端口数 | `NUM_CONCURRENT_PORT` | 内核签名/pragma、ini 的 sp 行数、主机 NUM_PORT |
| 端口位宽 | `PORT_WIDTH` | `krnl_config.h` 的 DWIDTH、主机 dataSize 换算 |
| 最大突发长度 | `MAX_BURST_LENGTH` | 内核 m_axi pragma |
| 连续访问数据量 | `CONSECUTIVE_DATA_SIZE` | 主机 payload 循环边界 |

**跨工具契约（u1-l4/u3-l3）**。手写工程里，「内核参数顺序 ↔ 主机 setArg 编号 ↔ ini 的 sp 端口名」三处必须人工对齐，错了没有任何编译期检查。生成器的价值正在于此：三个文件由同一次循环迭代里的同一组变量派生，契约由构造保证。

**Python 2 与 Python 3 的差异（本讲需要的三点）**：

- Py2 的 `print "x"` 是语句，Py3 里是语法错误，必须写成 `print("x")`；
- Py2 的 `/` 对两个整数做整除（`1024/4 == 256`），Py3 做真除法（`1024/4 == 256.0`），整除要显式写 `//`；
- Py2 脚本惯例用 `#!/usr/bin/python` 作解释器声明，现代系统上该路径可能不存在，推荐 `#!/usr/bin/env python3`。

## 3. 本讲源码地图

| 文件 | 作用 | 对外接口（唯一函数） |
|---|---|---|
| [auto_collect/kernelcode_gen.py](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py) | 生成 `krnl_config.h` 与 `krnl_ubench.cpp` | `generateKernelCode(access_type, num_concurrent_port, port_width, max_burst_length)` |
| [auto_collect/hostcode_gen.py](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py) | 生成 `host.cpp`（含 bank 绑定与 payload 扫描） | `generateHostCode(access_type, num_concurrent_port, port_width, start, stop, bank_flag)` |
| [auto_collect/connectivity_gen.py](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/connectivity_gen.py) | 生成 `ubench.ini`（slr/sp/nk） | `generateConnectivity(access_type, num_concurrent_port, bank_name)` |
| [auto_collect/makefile_gen.py](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py) | 生成 `Makefile`（唯一变量是频率） | `generateMakefile(kernel_freq)` |
| [auto_collect/generate_microbenchmarks.py](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py) | 调用方：六层循环、目录命名、runAll.sh | 无（顶层脚本） |
| [auto_collect/config.py](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/config.py) | 参数空间定义 | 数据，无函数 |

四个生成器共同采用同一套朴素的代码生成技术：**把目标文件的每一行当作一个 Python 字符串，追加进 list，最后 `writelines` 一次性写出**。没有模板引擎、没有格式化占位符，全部是字符串字面量拼接——这意味着「生成的代码长什么样」完全等价于「脚本里的字符串字面量长什么样」，逐行读脚本就等于逐行读产物。

## 4. 核心概念与源码讲解

### 4.1 内核生成器 kernelcode_gen.py

#### 4.1.1 概念说明

`kernelcode_gen.py` 负责五件套中最核心的两件：契约头 `krnl_config.h` 与 HLS 内核 `krnl_ubench.cpp`。它解决的问题：手写方式下，改端口数要在内核签名、pragma、循环体三处同步增删（u3-l2 的「四处联动」），而这里只需 `for port_index in range(num_concurrent_port)` 一个循环就把三处一起生成；访问类型 RD/WR 决定指针名前缀（`in`/`out`）与突发 pragma 名（`max_read_burst_length`/`max_write_burst_length`）。

#### 4.1.2 核心流程

```
generateKernelCode(access_type, num_concurrent_port, port_width, max_burst_length)
 ├─ generateKernelConfigCode(port_width)     # 先写 krnl_config.h
 │    └─ 固定四行：DWIDTH=port_width, INTERFACE_WIDTH, NUM_ITERATIONS=10000
 ├─ 拼内核签名：for 每个端口 append "volatile INTERFACE_WIDTH* in{i}," 或 "out{i},"
 │    └─ 尾部固定补 "const int size," 与 "int* sum"
 ├─ 拼 pragma：for 每个端口 append m_axi(bundle=gmem{i}, 突发长度) + s_axilite
 │    └─ sum 端口单独配 m_axi bundle=gmem{N}（多出一个口！）
 ├─ 固定 append DATAFLOW、临时变量声明、for 每个端口的双层测量循环
 └─ 写出 krnl_ubench.cpp
```

注意 `krnl_config.h` 的生成内容与手写版有一个实质差异：**没有 `WIDTH_FACTOR`**（手写版有 `const int WIDTH_FACTOR = DWIDTH/32;`）。生成版把 `port_width/32` 这个换算以字面量形式直接写进主机代码（见 4.2.3），位宽信息在两处之间靠生成时的同一变量传递，而非靠头文件宏共享。

#### 4.1.3 源码精读

**契约头生成**——四行固定内容，只有 DWIDTH 是变量（当前 HEAD 的 config 默认 `PORT_WIDTH=[128]`，这里以实践任务的 512 为例）：

- [kernelcode_gen.py:L13-L15](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L13-L15)：依次生成 `const int DWIDTH = <port_width>;`、`#define INTERFACE_WIDTH ap_uint<DWIDTH>`、`const int NUM_ITERATIONS = 10000;`。对照手写版 [read/DDR/2ports_512bit/src/krnl_config.h:L4-L7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L4-L7)，缺少 `WIDTH_FACTOR` 一行。

**签名拼接**——端口数变成循环次数，RD/WR 决定指针方向：

- [kernelcode_gen.py:L34-L43](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L34-L43)：每个端口追加一行 `volatile INTERFACE_WIDTH* in{i},`；循环结束后固定追加 `const int size,` 和 `int* sum`。也就是说**生成版内核比手写版多一个 `sum` 输出端口**——手写版签名到 `size` 为止（见 [read/DDR/2ports_512bit/src/krnl_ubench.cpp:L4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L4)）。

**pragma 拼接**——五个带宽因素中「突发长度」的唯一落点：

- [kernelcode_gen.py:L46-L54](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L46-L54)：RD 分支生成 `#pragma HLS INTERFACE m_axi port=in{i} offset=slave bundle=gmem{i} max_read_burst_length=<burst>`，WR 分支换成 `out{i}` 与 `max_write_burst_length`。bundle 名 `gmem{i}` 随端口下标递增，异名即独立并发端口（u2-l1 的规则在这里被机械化）。
- [kernelcode_gen.py:L56-L59](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L56-L59)：`size` 只配 s_axilite；`sum` 额外配 `m_axi ... bundle=gmem{num_concurrent_port}`——2 端口设计的生成内核实际有 **3 个 m_axi 口**（gmem0/gmem1/gmem2）。

**防优化策略**——生成版与手写版路线不同：

- [kernelcode_gen.py:L64-L74](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L64-L74)：RD 分支声明 `ap_uint<DWIDTH> temp_data_{i}`（**非 volatile**）与 `int temp_sum_{i} = 0;`。手写版用的是 `volatile INTERFACE_WIDTH temp_data_0;`（[read/DDR/2ports_512bit/src/krnl_ubench.cpp:L14-L15](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L14-L15)）——两版防死代码消除的手段不同：手写版靠 volatile 临时变量，生成版靠「把读到的值累加后经 sum 端口写回内存」。
- [kernelcode_gen.py:L77-L84](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L77-L84)：RD 测量循环体内 `temp_data_{i} = in{i}[j];` 之后接 `ap_int<32> temp_int = temp_data_{i}.range(31,0); temp_sum_{i} += temp_int;`——取低 32 位累加，消费掉读到的数据。`NUM_ITERATIONS` 外层放大与 `PIPELINE II=1` 与手写版一致。
- [kernelcode_gen.py:L96-L97](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L96-L97)：循环结束后 `sum[{i}] = temp_sum_{i};` 把累加结果写回，这就是 sum 端口存在的意义。

**一个值得注意的脚本缺陷**：[kernelcode_gen.py:L66](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L66) 生成的 RD 声明行 `'        ap_uint<DWIDTH> temp_data_' + str(port_index) + '\n'` **末尾没有分号**（对照 WR 分支 L68 的 `' = 100;'` 是有的）。端口数 ≥ 2 时两行声明相邻拼接，产物是 `ap_uint<DWIDTH> temp_data_0   ap_uint<DWIDTH> temp_data_1`，C++ 语法错误，无法通过 v++ 编译。这提示：生成器本身也需要测试，字符串拼接漏一个字符就是全线报错。（单端口设计两行不相邻拼接时同样缺分号，仍无法编译；此结论由字符串字面量直接推出，待本地运行生成后验证。）

由脚本机械推导，RD/2 端口/64 突发的生成内核预览（示例代码，即脚本将产出的内容）：

```cpp
extern "C" {
    void krnl_ubench(
        volatile INTERFACE_WIDTH* in0,
        volatile INTERFACE_WIDTH* in1,
        const int size,
        int* sum
    ) {
        #pragma HLS INTERFACE m_axi port=in0 offset=slave bundle=gmem0 max_read_burst_length=64
        #pragma HLS INTERFACE s_axilite port=in0 bundle=control
        #pragma HLS INTERFACE m_axi port=in1 offset=slave bundle=gmem1 max_read_burst_length=64
        #pragma HLS INTERFACE s_axilite port=in1 bundle=control
        #pragma HLS INTERFACE s_axilite port=size bundle=control
        #pragma HLS INTERFACE m_axi port=sum offset=slave bundle=gmem2
        ...
#pragma HLS DATAFLOW

        ap_uint<DWIDTH> temp_data_0        // 注意：脚本生成的这一行缺分号
        ap_uint<DWIDTH> temp_data_1
        int temp_sum_0 = 0;
        int temp_sum_1 = 0;
        // 每端口：NUM_ITERATIONS × size 的双层循环，II=1，
        // 循环体 temp_data_i = in_i[j]; temp_sum_i += .range(31,0);
        sum[0] = temp_sum_0;
        sum[1] = temp_sum_1;
        return;
    }
}
```

#### 4.1.4 代码实践

1. **实践目标**：不经运行，手工推演生成器输出。
2. **操作步骤**：以 `access_type='RD', num_concurrent_port=2, port_width=512, max_burst_length=64` 为入参，拿一张纸，从 [kernelcode_gen.py:L29](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L29) 开始逐个 append 语句抄下产物行；特别标记 L34、L48、L66、L77-84、L97 这五处循环体各自展开成哪几行。
3. **需要观察的现象**：哪些行随 `port_index` 变化、哪些行固定；`sum` 端口的 bundle 编号为什么是 `gmem2` 而不是 `gmem0`。
4. **预期结果**：得到一份约 30 行的内核源码草稿，其中 `temp_data_` 两行无分号；随后把它与你今后真正运行脚本得到的 `krnl_ubench.cpp` 对比应逐字符一致。
5. 本地无 Python 环境时，此推演本身即为「源码阅读型实践」，结论可直接从字符串字面量读出，无需运行验证。

#### 4.1.5 小练习与答案

**练习 1**：把 `num_concurrent_port` 从 2 改为 4，生成内核会有几个 m_axi 端口？分别叫什么？
**答案**：5 个。`in0..in3` 各配 `gmem0..gmem3`，外加 `sum` 配 `gmem4`（[kernelcode_gen.py:L57](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L57) 用 `str(num_concurrent_port)` 拼下标）。

**练习 2**：生成版与手写版的防优化手段有何不同？各自的代价是什么？
**答案**：手写版用 `volatile` 临时变量承接读值，代价是每端口一个宽寄存器且编译器不得优化对其访问；生成版用非 volatile 临时变量 + `range(31,0)` 截取累加 + 经 sum 端口写回，读值被真实消费，代价是多占一个 m_axi 口（且该口未被 connectivity_gen 显式绑定，见 4.2.3）。

**练习 3**：为什么生成版 `krnl_config.h` 可以不要 `WIDTH_FACTOR`？
**答案**：`WIDTH_FACTOR` 的唯一用途是主机把 int 个数换算成宽字个数；生成版把这个换算以 `port_width/32` 字面量直接写进 host.cpp（4.2.3），两处来自同一次函数调用的同一变量，无需头文件宏再来共享一次。

### 4.2 主机与连接生成器 hostcode_gen.py / connectivity_gen.py

#### 4.2.1 概念说明

`hostcode_gen.py` 生成约 230 行的 `host.cpp`，是四个生成器中唯一有大段 RD/WR 分支的：RD 生成「READ_ONLY 缓冲 + 迁移输入数据」，WR 生成「WRITE_ONLY 缓冲、不迁移」。它把参数空间中最后两个因素落进代码：`CONSECUTIVE_DATA_SIZE` 换算成 payload 循环边界；`MEMORY_TYPE['BANK_FLAG']` 注入 `cl_mem_ext_ptr_t.flags`。`connectivity_gen.py` 只有三行有效逻辑，生成链接期连接表 `ubench.ini`，与主机 bank flag 构成「跨工具契约」的两端——在手写工程里这两端要人工对齐（u3-l3），在这里由同一循环变量保证一致。

#### 4.2.2 核心流程

```
generateHostCode(access_type, num_port, port_width, start_KB, stop_KB, bank_flag)
 ├─ 固定头 + NUM_KERNEL=1 + NUM_PORT=num_port
 ├─ payload 循环：for (int payload(start_KB*1024/4); payload <= stop_KB*1024/4; payload*=2)
 ├─ 按 RD/WR 二选一生成缓冲段：
 │    ├─ 主机源向量（aligned_allocator）
 │    ├─ NUM_KERNEL*NUM_PORT 个 cl_mem_ext_ptr_t，flags = bank_flag
 │    ├─ sum 缓冲：NUM_KERNEL 个普通 cl::Buffer（无 ext_ptr、无 bank 绑定）
 │    └─ 数据缓冲：RD 为 READ_ONLY+迁移，WR 为 WRITE_ONLY+不迁移
 ├─ 计时启动 → dataSize = dataSize/(port_width/32) → setArg 双层循环 → enqueueTask → finish
 └─ 带宽公式（魔数 0.000010000）→ 输出 bw_result

generateConnectivity(access_type, num_port, bank_name)
 ├─ [connectivity] + slr=krnl_ubench_1:SLR0（写死）
 ├─ for 每个端口：sp=krnl_ubench_1.in{i}:<bank_name>
 └─ nk=krnl_ubench:1（写死）
```

payload 边界换算：config 以 KB 为单位（`START_SIZE:1, STOP_SIZE:1024`），生成器除以 4 变成 int 个数：

\[ \text{payload}_{start} = \text{START\_KB} \times 1024 / 4 = \text{START\_KB} \times 256 \]

默认配置即 256 → 262144 个 int、11 档倍增，与手写版 [host.cpp:L100](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L100) 完全一致。

#### 4.2.3 源码精读

**payload 扫描边界的注入点**：

- [hostcode_gen.py:L95](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L95)：`for (int payload(' + str(consecutive_data_start_size*1024/4) + '); payload <= ' + str(consecutive_data_stop_size*1024/4) + '; payload*=2){`——`CONSECUTIVE_DATA_SIZE` 只改这一行的两个数字，不增加目录数（u5-l1 的「范围字典」设计在此落地）。注意表达式里的 `/` 是 Python 2 整除，迁移 Python 3 时有语义陷阱，见 4.4。

**bank flag 的注入点**：

- [hostcode_gen.py:L113-L126](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L113-L126)：`is_emulation()` 分支与 `else` 分支生成的代码**逐行相同**，都写 `source_in0_ext[i].flags = <bank_flag>;`——手写版那个「模板遗留空骨架」（u2-l2）被原样复制进了生成器。当前 [config.py:L13-L14](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/config.py#L13-L14) 中 DDR 与 HBM 的 `BANK_FLAG` 都是字符串 `'0 | XCL_MEM_TOPOLOGY'`（拓扑风格、bank 0），而手写 DDR 版用的是 `XCL_MEM_DDR_BANK1` 宏（[host.cpp:L119](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L119)）——同一片 DDR，两种写法，这正是 diff 实践中要解释的差异之一。

**RD/WR 分支的结构差异**：

- [hostcode_gen.py:L103-L147](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L103-L147)：RD 段生成 `CL_MEM_READ_ONLY | CL_MEM_EXT_PTR_XILINX | CL_MEM_USE_HOST_PTR` 缓冲，且每个缓冲后跟 `enqueueMigrateMemObjects`（数据搬运不计时，u1-l4 的约定）。
- [hostcode_gen.py:L148-L187](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L148-L187)：WR 段生成 `CL_MEM_WRITE_ONLY` 缓冲，**没有**迁移调用——与手写 write 版一致（u3-l4）。
- 两段共同的 [L131-L133](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L131-L133)：`source_outSum_buffer[i] = cl::Buffer(context, CL_MEM_WRITE_ONLY, sizeof(int)*NUM_PORT);`——sum 缓冲是**普通 Buffer，不带 ext_ptr**，与内核侧 sum 端口未在 ini 里绑定 sp 相互呼应：sum 走默认内存通道，不参与测量。

**setArg 编号契约**：

- [hostcode_gen.py:L200-L217](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L200-L217)：先 `dataSize = dataSize / (512/32);`（位宽换算以字面量内联），再双层循环 `setArg(j, source_..._buffer[i*NUM_PORT+j])`，随后 `setArg(j, dataSize)`、`setArg(j+1, source_outSum_buffer[i])`、`enqueueTask`。编号顺序严格对应 kernelcode_gen 拼出的签名 `in0..in{N-1}, size, sum`——两个生成器各写一半契约，靠参数一致拼合。

**带宽公式**（与手写版逐字符相同，u2-l3 已推导）：

- [hostcode_gen.py:L224](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L224)：`double bw_result = payload * 4 * 0.000010000 / kernel_time_in_sec * NUM_KERNEL * NUM_PORT;`，即

\[ bw = \frac{\text{payload} \times 4 \times 10^{-5}}{t} \times N_{kernel} \times N_{port} \]

其中 \(10^{-5} = \text{NUM\_ITERATIONS}/10^9\) 被硬编码为魔数；生成版 `NUM_KERNEL` 恒为 1（[L22](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L22)），故并发倍率全靠 `NUM_PORT`。

**连接表生成**：

- [connectivity_gen.py:L10-L11](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/connectivity_gen.py#L10-L11)：`[connectivity]` 与 `slr=krnl_ubench_1:SLR0`——SLR 写死为 0；手写 read/DDR 版是 `slr=krnl_ubench_1:SLR1`（[ubench.ini:L2](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L2)）。
- [connectivity_gen.py:L13-L19](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/connectivity_gen.py#L13-L19)：每个端口一行 `sp=krnl_ubench_1.in{i}:<bank_name>`。`bank_name` 来自 `MEMORY_TYPE['BANK_NAME']`（DDR→`DDR[0]`，HBM→`HBM[0]`），**所有端口共享同一通道**——与手写版「双端口接 DDR[1]」一样测的是共享通道争用（u3-l3）。注意它只为 `in{i}` 生成 sp 行，内核多出来的 `sum` 口没有 sp 行，由 v++ 分配默认连接。
- [connectivity_gen.py:L21](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/connectivity_gen.py#L21)：`nk=krnl_ubench:1` 写死单实例——生成版永远「1 内核 × N 端口」，而手写 write 版是「2 内核 × 1 端口」（u3-l4 的转置结构）。目录名里的 `Nport` 在两套体系里含义不同：生成版是每核端口数，手写 write 版是总端口数。

#### 4.2.4 代码实践

1. **实践目标**：验证 payload 换算与 sp 行数随参数的扩张规律。
2. **操作步骤**：
   - 在纸上把 `CONSECUTIVE_DATA_SIZE` 改为 `{'START_SIZE':4, 'STOP_SIZE':64}`，按 4.2.2 公式计算生成 host.cpp 中 `for (int payload(...)` 一行的完整文字。
   - 数一数 `NUM_CONCURRENT_PORT=4` 时 connectivity_gen 会生成几行 sp、内核签名有几行指针、host 的 `setArg(j, ...)` 循环体执行几次。
3. **需要观察的现象**：payload 起点 4KB → 1024 个 int；sp 行数 = 端口数 = setArg 的 buffer 次数，三者恒相等。
4. **预期结果**：`for (int payload(1024); payload <= 16384; payload*=2){`；sp 4 行；setArg 循环 4 次 buffer + 1 次 size + 1 次 sum。
5. 若本地有 python3，可临时改 config 后运行生成脚本核对（注意先处理 4.4 的迁移问题）；否则本推演即结果。

#### 4.2.5 小练习与答案

**练习 1**：为什么 hostcode_gen 的 `is_emulation()` 与 `else` 两分支生成相同代码还有存在的价值？
**答案**：没有技术价值，是手写模板遗留的空骨架被复制进生成器；它的存在反而是阅读线索——提醒你手写版里曾经想区分仿真/真机的 bank flag（u2-l2），生成版用 `BANK_FLAG` 参数把两处统一了。

**练习 2**：生成 RD 设计的所有端口都接 `DDR[0]`、主机 flags 为 `0 | XCL_MEM_TOPOLOGY`，这两处若不一致会发生什么？
**答案**：编译期无任何报错；运行期数据落在与内核连线不同的内存通道，读到的数据错位或访问无效地址，错误滞后暴露——这就是 u3-l3 所说「无编译期检查的跨工具契约」，也正是生成器用同一 `MEMORY_TYPE` 字典同时喂 `BANK_NAME`（ini）与 `BANK_FLAG`（host）来消除的隐患。

**练习 3**：内核有 `sum` 口但 ini 没有 `sp=...sum...` 行，主机 sum 缓冲也没绑 bank，这会导致什么？
**答案**：sum 端口由链接器分配默认内存连接，功能上仍可写回（只是落在未指定的通道）；由于 sum 只在测量结束后写一次 `NUM_PORT` 个 int，其流量可忽略，不影响带宽测量。

### 4.3 Makefile 生成器 makefile_gen.py

#### 4.3.1 概念说明

`makefile_gen.py` 是四个生成器里最「参数无关」的：唯一入参 `kernel_freq` 只出现在一行 `CLFLAGS`。它生成的 Makefile 本体是 Vitis 2020.2 官方示例 Makefile 的逐行复刻（help 文案、`check-devices`、v++ 编译链接规则、`check` 目标、clean 等），因此读懂它约等于读懂 u1-l3 讲过的手写版 Makefile——差异集中在两处：`COMMON_REPO` 的相对深度和 `--kernel_frequency`。

#### 4.3.2 核心流程

```
generateMakefile(kernel_freq)
 ├─ help 文案（固定 20+ 行）
 ├─ COMMON_REPO = ../../../../../../   ← 6 级上溯
 ├─ include $(ABS_COMMON_REPO)/common/utils.mk + opencl.mk + xcl2.mk
 ├─ CLFLAGS += -t $(TARGET) --platform $(DEVICE) --save-temps --kernel_frequency <freq>
 ├─ LDCLFLAGS += --config ./ubench.ini
 ├─ v++ -c（.xo）与 v++ -l（.xclbin）规则、host 编译规则
 └─ check 目标：仿真则设 XCL_EMULATION_MODE 运行，真机直接运行，末尾 perf_analyze
```

#### 4.3.3 源码精读

**频率因素的唯一下注点**：

- [makefile_gen.py:L75](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py#L75)：`CLFLAGS += -t $(TARGET) --platform $(DEVICE) --save-temps --kernel_frequency ' + str(kernel_freq)`——`KERNEL_FREQ` 维度从 config 到 v++ 命令行的完整链路只有这一处拼接。手写版 Makefile **没有** `--kernel_frequency`（[read/DDR/2ports_512bit/Makefile:L66](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L66) 的 CLFLAGS 到 `--save-temps` 为止），频率由平台时序约束默认值决定——这就是 u3-l2 指出的「README 提到 `--kernel_frequency` 但只在 auto_collect 生成的 Makefile 里存在」。

**COMMON_REPO 深度——生成版与手写版的关键差异**：

- [makefile_gen.py:L38](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py#L38)：`COMMON_REPO = ../../../../../../`（6 级）。生成目录位于 `<仓库根>/ubench/offchip_bandwidth/datacenter/auto_collect/uBenchDesignDir/<设计名>/`，距根正好 6 层，6 级上溯**精确落在仓库根**，随后的 [L46-L57](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py#L46-L57) `include $(ABS_COMMON_REPO)/common/utils.mk`、`opencl.mk`、`xcl2.mk` 均能解析到真实文件（已按仓库当前布局核算）。
- 手写版 [read/DDR/2ports_512bit/Makefile:L29](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L29) 写的是 5 级 `../../../../../`，从 `read/DDR/2ports_512bit/` 上溯 5 层落在 `ubench/` 目录，而 `common/` 只存在于仓库根（`ubench/common` 不存在，已实测）——按当前仓库布局直接 `make` 会在 include opencl.mk 处因找不到文件而中止，需把 5 级改为 6 级对齐。生成版算对了深度，这是自动化生成优于手工拷贝的一个直接证据（也更说明 diff 实践的价值）。

**其余部分是手写版的同构复刻**：

- [makefile_gen.py:L82](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py#L82)：`LDCLFLAGS += --config ./ubench.ini`——连接表经 v++ -l 的 `--config` 进入链接期（u3-l3）。
- [makefile_gen.py:L104-L109](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py#L104-L109)：`v++ -c -k krnl_ubench` 产出 `.xo`、`v++ -l` 产出 `ubench.xclbin` 的两条规则，与 u1-l3 的构建链一致。
- [makefile_gen.py:L119-L144](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py#L119-L144)：`check` 目标在 sw_emu/hw_emu 下设 `XCL_EMULATION_MODE` 后运行可执行文件，末尾 [L143](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py#L143) 的 `perf_analyze profile -i profile_summary.csv -f html` 为 u7-l3 的剖析流程留下入口。

#### 4.3.4 代码实践

1. **实践目标**：掌握 COMMON_REPO 深度核算方法。
2. **操作步骤**：从生成设计目录 `<根>/ubench/offchip_bandwidth/datacenter/auto_collect/uBenchDesignDir/X/` 出发，逐级列出 `../`×1..6 各落在哪个目录；再对手写目录 `read/DDR/2ports_512bit/` 做 `../`×1..5 同样练习；最后用 `readlink -f` （或 `ls`）验证 `common/utils.mk` 在两个基准下的可达性。
3. **需要观察的现象**：6 级上溯到达仓库根后 `common/` 存在；5 级到达 `ubench/` 后其下没有 `common/`。
4. **预期结果**：生成版 Makefile 的三个 include 路径全部可解析；手写版需要把 `COMMON_REPO` 改成 6 级才能在当前布局下工作。
5. 本练习纯路径运算 + 文件系统检查，不依赖 Vitis，可直接本地验证。

#### 4.3.5 小练习与答案

**练习 1**：五个带宽因素中哪一个只影响 Makefile？改它要不要动其他生成器？
**答案**：内核频率（`KERNEL_FREQ`）。只影响 `CLFLAGS` 的 `--kernel_frequency`；频率变化会改变理论峰值 \( f \times N_{port} \times W / 8 \)，但其他生成器生成的源码不含频率，无需联动。

**练习 2**：为什么生成版 Makefile 需要 6 级而手写版写 5 级？
**答案**：目录深度不同：生成设计目录比手写目录深 2 层（多了 `auto_collect/uBenchDesignDir`），但也少了 `read/DDR` 的 2 层，净差 1 层（6 对 5）。手写版 5 级在当前布局下落不到仓库根，是目录重组后未同步的滞后项。

**练习 3**：若想把生成设计挪到 `uBenchDesignDir` 之外的自定义目录，Makefile 哪一行必须变？
**答案**：[makefile_gen.py:L38](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py#L38) 生成的 `COMMON_REPO` 相对深度——或者干脆在生成后的 Makefile 里把它改成绝对路径；这也是生成器把路径写死带来的可移植性代价。

### 4.4 Python 2 → Python 3 迁移

#### 4.4.1 概念说明

这套脚本写于 Python 2 时代（shebang `#!/usr/bin/python`）。**精确地说：四个 `*_gen.py` 本身的语法在 Python 3 下都能解析**——它们的 `print('...')` 是单参数调用形式，Py2/Py3 双兼容；**全目录唯一的 Py3 语法错误在主脚本** `generate_microbenchmarks.py` 末尾的裸 `print` 语句。但「能解析」不等于「行为相同」：`hostcode_gen.py` 里的整数除法在 Py3 下语义改变，会悄悄污染生成的 C++ 代码。迁移要同时处理语法层与语义层。

#### 4.4.2 核心流程

迁移清单（按优先级）：

| # | 位置 | Py2 现状 | Py3 问题 | 修法 |
|---|---|---|---|---|
| 1 | [generate_microbenchmarks.py:L91](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L91) | `print "Microbenchmark Generation Done!"` | SyntaxError，主脚本直接无法运行 | 改 `print("...")` |
| 2 | [hostcode_gen.py:L95](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L95) | `start*1024/4` 整除得 int | Py3 真除法得 float，`str()` 生成 `"256.0"` | 改 `//`（或 `int(...)`） |
| 3 | 五个文件 L1 | `#!/usr/bin/python` | 现代系统可能无 `/usr/bin/python` | 改 `#!/usr/bin/env python3` |
| 4 | [generate_microbenchmarks.py:L16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L16) | `os.mkdir(uBenchDesignDir)` | 目录已存在即 OSError，脚本不可重跑 | `os.makedirs(..., exist_ok=True)`（可选增强） |

其中第 2 项最隐蔽：`1*1024/4` 在 Py2 是 `256`，在 Py3 是 `256.0`；生成的 C++ 变成 `for (int payload(256.0); payload <= 262144.0; ...)`。多数 C++ 编译器会接受（int 直接初始化自 double，仅告警），但这不是脚本作者意图的产物，且一旦日后有人把边界拿去做宏或断言就会炸——迁移时必须显式改成 `//`。

另有两点不影响正确性但值得知道：`from config import *` 等同级导入在 Py3 下要求**从 auto_collect 目录内启动脚本**（脚本所在目录会被加入 `sys.path`，u5-l1 的运行纪律恰好满足）；`generate_microbenchmarks.py` 顶部导入的 `subprocess`、`shutil` 与文件尾部大段被注释的 KNN 遗留代码（[L74-L89](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L74-L89)）均未使用，可顺手清理。

#### 4.4.3 源码精读

- [generate_microbenchmarks.py:L91](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L91)：`print "Microbenchmark Generation Done!"`——唯一的裸 print 语句。迁移后：`print("Microbenchmark Generation Done!")`。
- [hostcode_gen.py:L95](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L95)：迁移后写法（示例代码）：

```python
# Python 3
'    for (int payload(' + str(consecutive_data_start_size*1024//4) + '); payload <= ' \
+ str(consecutive_data_stop_size*1024//4) + '; payload*=2){'
```

- [kernelcode_gen.py:L40](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L40)、[hostcode_gen.py:L189](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L189)、[connectivity_gen.py:L19](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/connectivity_gen.py#L19)：三处 `print('... : Invalid access type')` 均为单参数调用，Py2/Py3 双兼容，**无需修改**——迁移时不要盲目全改，改对了才加分。

#### 4.4.4 代码实践

1. **实践目标**：用工具精确定位语法不兼容点。
2. **操作步骤**：在 `auto_collect` 目录执行 `python3 -m py_compile config.py kernelcode_gen.py hostcode_gen.py connectivity_gen.py makefile_gen.py generate_microbenchmarks.py`（只读源码、不生成目录，不违反「不可修改源码」——`py_compile` 仅在内存中解析；如担心产物可在自己的临时目录里对副本执行）。
3. **需要观察的现象**：前五个文件编译通过；`generate_microbenchmarks.py` 报 `SyntaxError: Missing parentheses in call to 'print'` 并指向 L91。
4. **预期结果**：与 4.4.2 清单第 1 条吻合；再对 `hostcode_gen.py` 交互式验证 `1*1024/4` 在 python3 下输出 `256.0`、`1*1024//4` 输出 `256`。
5. 待本地验证（依赖本地装有 python3）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `print('Invalid access type')` 不需要迁移而 `print "Done!"` 必须？
**答案**：前者在 Py2 里被解析为 `print` 语句加一个括号表达式，输出与 Py3 函数调用完全一致；后者是裸 print 语句，Py3 语法直接拒绝。判别标准是「写了括号没有」，不是「用没用了 print」。

**练习 2**：迁移后忘了改 `/` 为 `//`，脚本会崩溃吗？会造成什么后果？
**答案**：不会崩溃，这正是危险之处——生成的 C++ 里出现 `int payload(256.0)`，多数编译器仅告警放行，错误被推迟到编译甚至运行阶段才显形，属于典型的「静默语义漂移」。

**练习 3**：除了语法与除法，从工程角度还应补什么？
**答案**：至少三点——`os.makedirs(exist_ok=True)` 让脚本可重跑；给 4.1.3 发现的「RD 临时变量缺分号」补上分号并加一个「生成后立即 `gcc -fsyntax-only` 冒烟检查」；把 runAll.sh 里写死的绝对路径改为相对路径以提升可移植性。

## 5. 综合实践

**任务**：完成四个生成器的 Python 3 迁移，生成一个指定设计，并与手写工程做全量 diff。（全程在自己的副本目录操作，不动仓库源码。）

**步骤**：

1. **复制工作区**：把 `auto_collect/` 整体复制到仓库外的临时目录（保持五个 .py 同级），同时复制手写工程 `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/` 留作对照。
2. **迁移**：按 4.4.2 清单修改副本——`print` 加括号、`/` 改 `//`、shebang 换 `env python3`、`os.mkdir` 改 `os.makedirs(..., exist_ok=True)`；顺手为 [kernelcode_gen.py:L66](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L66) 的 RD 声明行补上分号。
3. **配置单点设计**：把副本 `config.py` 改为：

```python
KERNEL_FREQ = [300]
NUM_CONCURRENT_PORT = [2]
PORT_WIDTH = [512]
MAX_BURST_LENGTH = [64]
CONSECUTIVE_DATA_SIZE = {'START_SIZE':1, 'STOP_SIZE':1024} # in KB
ACCESS_TYPE = ['RD']
MEMORY_TYPE = [{'BANK_TYPE':'DDR', 'BANK_FLAG':'0 | XCL_MEM_TOPOLOGY',
                'BANK_NAME':'DDR[0]', 'DEVICE_NAME':'xilinx_u200_xdma_201830_2'}]
```

4. **生成**：在副本 auto_collect 目录内运行 `python3 generate_microbenchmarks.py`，确认只产生一个目录 `RD_DDR_300MHz_2port_512bit_64max_burst_length`（目录名由 [generate_microbenchmarks.py:L29-L32](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L29-L32) 的拼接规则决定），其下有 Makefile、ubench.ini、src/{host.cpp, krnl_ubench.cpp, krnl_config.h}，同级有 runAll.sh。
5. **diff 对照手写工程**，逐项解释以下预期差异（都已在正文核实）：

| 差异点 | 生成版 | 手写版 read/DDR/2ports_512bit |
|---|---|---|
| 内核签名 | `in0,in1,size,sum`（sum 为消费读值的累加写回口，bundle=gmem2） | `in0,in1,size`，无 sum |
| 防优化 | 非 volatile temp + `range(31,0)` 累加进 sum | `volatile` 临时变量 |
| krnl_config.h | 无 `WIDTH_FACTOR` | 有 `WIDTH_FACTOR = DWIDTH/32` |
| 突发长度 | 64（来自 config） | 16 |
| ini：slr | `SLR0`（写死） | `SLR1` |
| ini：sp | `DDR[0]`（来自 config BANK_NAME） | `DDR[1]` |
| 主机 bank flag | `0 | XCL_MEM_TOPOLOGY`（拓扑风格，来自 config） | `XCL_MEM_DDR_BANK1` |
| Makefile | `COMMON_REPO` 6 级（可达仓库根）、有 `--kernel_frequency 300` | 5 级（落在 `ubench/`）、无频率参数 |
| 计时/公式 | chrono + `payload*4*0.00001/t*NUM_KERNEL*NUM_PORT` | 相同 |

6. **重点解释「sum 端口」**：写一段 200 字左右的说明——它为什么存在（把读到的数据累加写回片外，替代 volatile 防止死代码消除）、它为何不影响测量（只在测量循环结束后写 NUM_PORT 个 int）、它为何没有 sp 行与 bank 绑定（连接交给默认分配）。
7. **可选验证**：若本机装有 Vitis，对生成工程执行 `make check TARGET=sw_emu DEVICE=<平台>` 验证功能链路；否则以 `gcc -fsyntax-only -I<src目录> src/host.cpp` 之类的主机侧语法检查替代，内核侧标注「待本地验证」。

**预期结果**：得到一份迁移补丁 + 一份 diff 差异清单 + sum 端口说明；diff 中除上表所列差异外，两版 host.cpp 的 OpenCL 骨架（设备遍历、Kernel 创建、setArg/finish、带宽公式）应逐字符同构。

## 6. 本讲小结

- 四个生成器共用「list 装字符串 → `writelines`」的朴素代码生成技术，读脚本字符串字面量就等于读生成产物；没有模板引擎，也就没有隐藏的变换。
- `kernelcode_gen.py` 把端口数变成循环次数，一次生成签名、pragma、临时变量与测量循环；生成版比手写版多一个 `sum` 写回口用于消费读值，`krnl_config.h` 里则省掉了 `WIDTH_FACTOR`（换算内联进主机）；L66 的 RD 声明缺分号是会在编译期暴露的脚本缺陷。
- `hostcode_gen.py` 落地最后两个因素：`CONSECUTIVE_DATA_SIZE`×256 变成 payload 循环边界、`BANK_FLAG` 注入 `cl_mem_ext_ptr_t`；`connectivity_gen.py` 三行有效逻辑生成 slr(写死 SLR0)/sp(全端口同一 bank)/nk(写死 1)，与主机 flag 构成由同一 `MEMORY_TYPE` 字典保证一致的跨工具契约。
- `makefile_gen.py` 唯一参数是频率，落点 `--kernel_frequency`；其 6 级 `COMMON_REPO` 恰好对齐仓库根，而手写版 5 级在当前布局下解析不到 `common/`。
- Python 迁移的精确清单：唯一语法错误是主脚本的裸 `print`；四个 gen 文件语法双兼容，但 `hostcode_gen.py` 的整数除法在 Py3 下产生 `256.0` 式的静默污染，须改 `//`。

## 7. 下一步学习建议

- 下一讲 **u5-l3 流带宽与嵌入式版本的 auto_collect 变体**：对比 streaming 版（无 ACCESS_TYPE/MAX_BURST_LENGTH 维度、新增 NUM_KERNEL）与 embedded 版（`MEMORY_TYPE` 用 HP/HPC 端口名替代 bank 名、频率降至 150MHz）的 config 差异，并归纳三套脚本共享的「config + generate + 生成器」架构模式——本讲读懂的四个生成器是那场对比的参照系。
- 若想立即动手，可回到本讲综合实践的第 7 步，用 sw_emu 跑通生成工程，再预习 u7-l3：用 `xrt.ini` profile 与 `perf_analyze` 检验主机计时与内核计时的差值。
- 源码层面建议复读 [generate_microbenchmarks.py:L22-L63](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L22-L63) 的六层循环体，自己画出「循环变量 → 生成文件 → 五因素」的完整映射图，作为 u7-l2「自建微基准接入 auto_collect」的设计底稿。
