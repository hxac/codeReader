# 片外延迟微基准：随机访问与延迟估计

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释「随机下标数组 + 二次遍历」这套机制如何制造出**地址相互依赖的串行随机访问链**，以及为什么必须把下标先搬进片上数组。
2. 说明内核里 `local_in0_index[524288]` 这个 magic number 的来历：它与「32bit 端口最大可索引 2MB 数据数组」之间的换算关系。
3. 从总时间推导平均随机访问延迟：\( \text{latency} = T / (\text{NUM\_ITERATIONS} \times \text{size}) \)，再折算成 ns/access，并指出仓库现有输出（打印成 "GB/s" 的 `Latency`）其实是个带宽口径的误标。
4. 识别本工程 README 与实际代码的一处不一致：README 的代码片段写 payload 从 256 起，实际代码从 16（即 64B）起。

本讲是 `ubench/offchip_latency/` 三类微基准线的最后一站：带宽基准（u3 系列）测的是「流水填满后每秒能搬多少字节」，延迟基准测的是「一次孤立访问要走多久」。两者共同刻画内存系统的完整画像。

## 2. 前置知识

### 2.1 带宽与延迟是两个独立的量

- **带宽（bandwidth）**：流水线打满时，单位时间搬运的数据量。u3 系列的读/写带宽内核用 `PIPELINE II=1` 让几十个读请求同时在飞，单个访问的延迟被并行度完全掩盖。
- **延迟（latency）**：从发出一次访问请求到数据返回的端到端时间。对 DDR4 来说，这个量通常是**百纳秒量级**（具体数值待本地验证），它由控制器排队、行激活、列选通、数据返回等多级环节组成。

一个反直觉的事实：带宽高的系统延迟不一定低（深流水可以同时做到「很高吞吐」和「很长延迟」），所以要单独测。

### 2.2 想测延迟，必须让访问「串行」且「随机」

如果连续两次访问之间没有依赖，工具链和内存控制器会自动把第二个请求提前发出（重叠执行），你测到的就是吞吐而不是延迟。因此延迟基准的核心手法是制造一条**依赖链**：第 \( j+1 \) 次访问必须等第 \( j \) 次访问完成才能发出。

同时访问地址必须是**随机**的：顺序访问会被合并成 AXI 突发（burst），把一次行激活的开销摊薄到几十拍上，测出来的是「突发摊薄后的有效延迟」而非单次随机访问延迟。

### 2.3 随机排列（洗牌）

主机端用 `std::random_shuffle` 把 \( 0,1,\dots,N-1 \) 打乱成一个**排列**（每个下标恰好出现一次）。这样遍历下标数组时，访问的数据地址就是均匀随机的，且保证整个数组被无重复地覆盖一遍。

### 2.4 与前序讲义的衔接

本讲假设你已掌握：u2-l1 的 `INTERFACE` pragma（`m_axi`/`bundle`/`s_axilite`）、u2-l2 的主机骨架（`setArg`/`enqueueTask`/`finish`、`cl_mem_ext_ptr_t` bank 绑定）、u3-l1 的读带宽内核与防优化技巧（`volatile`、`NUM_ITERATIONS`）。本工程的 `ubench.ini` 与 u3-l3 完全同构，不再逐条展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_config.h` | 契约头：`DWIDTH=32`、`WIDTH_FACTOR=1`、`NUM_ITERATIONS=10000`，被内核与主机共同 include |
| `ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_ubench.cpp` | HLS 内核：先把下标数组搬进片上，再按随机顺序反复访问数据数组 |
| `ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp` | OpenCL 主机：生成随机数据与洗牌下标、双缓冲绑定 DDR bank、计时并输出结果 |
| `ubench/offchip_latency/datacenter/DDR/32bit_per_access/ubench.ini` | 链接期连接配置：单内核实例，`in0` 与 `in0_index` 两个端口都连到 `DDR[0]` |
| `ubench/offchip_latency/datacenter/README.md` | 本微基准线的调参指南（有一处代码片段与实际代码不一致，见 4.3.3） |

工程仍是我们熟悉的「五件套」骨架（u1-l4），`Makefile` 与读带宽版结构一致（`-std=c++11` 编译主机、`v++ -l --config ./ubench.ini` 链接内核），目录名 `32bit_per_access` 表达的参数是「每次访问 32bit」——对比带宽线的 `2ports_512bit`，延迟线扫的是**位宽**与**数组大小**两个因素，没有端口数与突发维度（随机访问根本无法形成突发，扫突发没有意义）。

同目录下还有 `HBM/32bit_per_access` 变体。实测 `diff` 证明两版内核源码**仅差行尾空格**（逻辑完全相同），全部差异落在 `ubench.ini` 的 `sp` 行（`DDR[0]` → `HBM[0]`）与主机 bank flag 上——与 u3-l1、u3-l3 得出的「内存类型是链接期决策」结论一致。

## 4. 核心概念与源码讲解

### 4.1 随机访问模式：洗牌下标与地址依赖链

#### 4.1.1 概念说明

延迟测量的难点在于：**你不能直接「测一次访问的时间」**——FPGA 内核里没有读取时钟计数器再相减的便捷途径，而且单次访问太短，任何测量开销都会淹没它。

uBench 的做法是**放大再平均**：

1. 把数组下标 \( 0..N-1 \) 洗成一个随机排列，存成一张「访问顺序表」。
2. 内核按这张表把数组完整访问一遍——每个地址都是随机跳转。
3. 把第 2 步重复 `NUM_ITERATIONS`（10000）次。
4. 用总时间除以总访问次数，得到平均单次随机访问延迟。

这里有一个关键设计决策：**下标表必须先搬进片上存储**。如果内核直接写 `in0[in0_index[j]]`，那么每次随机访问之前都要先从 DDR 顺序读一个下标，两次访存串行，测出来的是「两次延迟之和」且混入了下标数组的访问模式。把下标放进片上数组后，取下标只要 1 个时钟周期，暴露出来的就纯粹是数据数组那一次随机访问的延迟。

#### 4.1.2 核心流程

```text
主机端（host.cpp）
  1. read_source[payload 个 int] ← std::rand 填充随机数据
  2. read_index[payload/WIDTH_FACTOR 个 int] ← 0,1,...,N-1
  3. std::random_shuffle(read_index)          ← 洗牌成随机排列
  4. 两个缓冲都迁移到 FPGA 的 DDR[0]
  5. setArg(0=in0, 1=in0_index, 2=size)
     其中 size = payload / WIDTH_FACTOR（宽字个数）
  6. enqueueTask → finish → 计时

内核端（krnl_ubench.cpp）
  阶段一（装载，顺序、可突发、PIPELINE II=1）
    for i in 0..size: local_in0_index[i] = in0_index[i]
  阶段二（测量，随机、不可突发、无流水指示）
    repeat NUM_ITERATIONS 次:
      for j in 0..size:
        temp = in0[ local_in0_index[j] ]   ← 地址来自片上表，随机跳转
```

延迟估计式：

\[
\text{latency (ns/access)} \;=\; \frac{T_{\text{sec}} \times 10^{9}}{\text{NUM\_ITERATIONS} \times \dfrac{\text{payload}}{\text{WIDTH\_FACTOR}}}
\]

其中 \( T_{\text{sec}} \) 是内核总执行时间（秒）。分母就是总访问次数：外层重复次数 × 每轮访问次数。

#### 4.1.3 源码精读

**内核签名与端口配置** —— [ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_ubench.cpp:L4-L11](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_ubench.cpp#L4-L11)

```cpp
void krnl_ubench(volatile INTERFACE_WIDTH* in0, int* in0_index, const int size ) {
#pragma HLS INTERFACE m_axi port=in0 offset=slave bundle=gmem0 max_read_burst_length=16 num_read_outstanding=16
#pragma HLS INTERFACE s_axilite port=in0 bundle=control
#pragma HLS INTERFACE m_axi port=in0_index offset=slave bundle=gmem1
```

这段做了三件事：

- `in0` 是数据端口，挂在 `bundle=gmem0`；`in0_index` 是下标端口，挂在**另一个** `bundle=gmem1`——两个端口物理分开，下标装载与数据访问不会争用同一个 AXI 通路。
- `in0` 上的 `max_read_burst_length=16` 和 `num_read_outstanding=16` 在这里其实是**模板遗留**：随机地址无法合并成突发（突发要求同一事务内地址连续），而依赖链也只让一个读在飞。它们对随机访问基本不生效，保留无害。
- `size` 与 `return` 走 `s_axilite` 的 `control` 寄存器组，对应主机 `setArg(2, dataSize)`——注意 setArg 编号契约：`in0`=0、`in0_index`=1、`size`=2，与签名顺序一致。

**真正的测量循环** —— [ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_ubench.cpp:L21-L25](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_ubench.cpp#L21-L25)

```cpp
    for (int i = 0; i < NUM_ITERATIONS; i++) {
        for (int j = 0; j < size; j++) {
            temp_data_0 = in0[local_in0_index[j]];
        }
    }
```

只有三行，但每个细节都为「测延迟」服务：

- **没有 `#pragma HLS PIPELINE`**。对比装载循环（L16-L19）明确写了 `PIPELINE II=1`，这里刻意不写。不加流水指示，HLS 默认把循环综合成顺序 FSM：第 \( j+1 \) 次迭代的读请求要等第 \( j \) 次迭代完成（`volatile` 写是不可重排的副作用，形成了次序约束）才发出——这正是我们要的串行依赖链。如果在这里加 `II=1`，多个访问会同时在飞，延迟被并行度掩盖，测到的就变回吞吐了。
- `temp_data_0` 是 `volatile`（L14），读到的值必须被消费，防止整个循环被死代码消除（u3-l1 讲过的双保险之一；这里的 `in0` 本身也是 `volatile` 指针）。
- `local_in0_index[j]` 是片上数组取数，1 拍可得，因此地址形成的时间不贡献额外访存延迟。

**主机端的洗牌** —— [ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp:L106-L113](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp#L106-L113)

```cpp
        std::vector<int, aligned_allocator<int>> read_source(dataSize);
        std::generate(read_source.begin(), read_source.end(), std::rand);

		std::vector<int, aligned_allocator<int>> read_index(dataSize/WIDTH_FACTOR);
        for (int i = 0; i < dataSize/WIDTH_FACTOR; ++i){
            read_index[i] = i;
        }
		std::random_shuffle(read_index.begin(), read_index.end());
```

- `read_source` 的**内容**其实无关紧要（内核只读不校验），填随机数只是习惯。
- `read_index` 先填成 \( 0..N-1 \) 再整体洗牌，得到一个**排列**——每个下标恰好出现一次。这保证一轮内层循环恰好把整个数组均匀覆盖一遍，没有热点、没有遗漏。
- 注意 `read_index` 的长度是 `dataSize/WIDTH_FACTOR`：下标的单位是「宽字」（`INTERFACE_WIDTH` 元素）而不是 int。本工程 `WIDTH_FACTOR=1`，两者相等；但如果你把 `DWIDTH` 改成 512，下标单位就变成 64B 的宽字（见 4.2.4 练习 3）。
- `std::random_shuffle` 随机种子来自 L95 的 `std::srand(std::time(NULL))`，每次运行的排列都不同。这个函数在 C++14 起被标为废弃、C++17 起被移除；本工程 Makefile 固定 `-std=c++11`（[Makefile:L53](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/Makefile#L53)）所以能编译，若你升级编译标准需换成 `std::shuffle` + `std::mt19937`。

**payload 扫描范围** —— [ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp:L98-L103](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp#L98-L103)

```cpp
    int dataSize(0);
    for (int payload(16); payload <= 262144; payload*=2){
        dataSize = payload;
        if (xcl::is_emulation()) {
            dataSize = 256; //1KB
        }
```

payload 从 **16** 起倍增到 262144，共 15 档。换算成字节数（int × 4B）就是 **64B → 1MB**。这就是讲义标题里提到的文档不一致点：README 正文说 "randomly accesses data array sizes from 64B to 1MB"（与代码一致），但它给出的示例代码片段却写 `payload(256)` 起——那个片段是滞后的，以 `src/host.cpp` 为准。仿真模式下 dataSize 被钉在 256（1KB），且仿真数值无物理意义（u1-l3 的结论同样适用）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「洗牌后的下标表」长什么样，并确认随机地址无法形成突发。

**操作步骤**：

1. 在 [host.cpp:L113](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp#L113) 的 `random_shuffle` 之后加一段打印（**示例代码**，非仓库原有）：

   ```cpp
   std::cout << "first 16 indices:";
   for (int i = 0; i < 16 && i < (int)read_index.size(); ++i)
       std::cout << " " << read_index[i];
   std::cout << std::endl;
   ```

2. 若本机装有 Vitis，用 `make check TARGET=sw_emu DEVICE=<平台>` 跑一遍（需要 `emconfig.json`，流程见 u1-l3）。
3. 若没有 Vitis 环境，可以写一个 10 行的纯 C++ 小程序（**示例代码**）：`vector<int> v(N); iota; random_shuffle; print`，直接在主机编译器上观察。

**需要观察的现象**：

- 打印出的 16 个下标杂乱无章，相邻两项之差时正时负、量级随机。
- 多跑几次（种子来自时间），每次序列都不同。

**预期结果**：相邻访问的地址差几乎从不等于 1（概率约 \( 2/N \)），因此 AXI 互连无法把相邻访问合并进同一个突发事务——每次访问都是独立事务，地址通道握手、行激活等开销逐次发生，这正是「随机访问延迟」的定义场景。加打印的版本**待本地验证**（打印只影响主机侧，不进入计时窗口，不影响测量）。

#### 4.1.5 小练习与答案

**练习 1**：如果给测量循环（内核 L22-L24）加上 `#pragma HLS PIPELINE II=1`，测出的「延迟」会怎么变？为什么？

**答案**：数值会显著变小，且不再是延迟。流水化后多个读请求同时在飞，单次访问延迟被并行度掩盖，总时间趋近 \( \text{访问次数} \times \text{II} \times \text{时钟周期} \)，测到的实质是随机访问吞吐。另外由于 `volatile` 写的次序约束，HLS 很可能根本达不到 II=1（会给出 II 违例告警）——这本身就说明该循环天然是串行的。

**练习 2**：为什么用「洗牌成排列」而不是「每次独立随机取一个下标」？

**答案**：排列保证一轮内层循环把数组每个元素恰好访问一次，访问地址在数组上均匀分布、无重复，统计上平均延迟无偏；独立随机取则会有的地址被访问多次、有的从不被访问，若内存控制器对近期访问过的行有 open-page 命中，结果会偏乐观且有方差。此外排列遍历的「下一地址」与「当前地址」无相关性，与真实随机负载一致。

**练习 3**：`in0` 端口上的 `max_read_burst_length=16` 在本内核里为什么基本不起作用？

**答案**：AXI 突发的前提是同一事务内地址连续递增。随机访问的相邻地址几乎从不连续，每次访问都只能发单 beat 事务，突发长度上限根本用不到。这个 pragma 是从读带宽内核模板复制来的遗留配置，删掉不改变行为（**待本地验证**：可用综合报告对比端口配置差异）。

### 4.2 下标缓冲：片上数组与 2MB 上限

#### 4.2.1 概念说明

内核里的 `local_in0_index[524288]` 是一张**片上**下标表。HLS 会把它综合进 BRAM/URAM（2MB 的容量通常会落到 URAM）。它的存在把「取下一个随机地址」这个动作从一次 DDR 访问降为一次片上读取，从而让测量循环里唯一的访存就是那一次随机读。

这个数组的大小不是随便写的，注释直接给出了推导：

```cpp
int local_in0_index[524288];// max 2MB data indexing for 32-bitwidth port
```

#### 4.2.2 核心流程

换算关系（32bit 端口，`WIDTH_FACTOR=1` 时）：

\[
524288 \;\text{个下标} \times 4\,\text{B/下标指向的字} \;=\; 2\,\text{MB}
\]

即片上表最多容纳 524288 个下标，每个下标指向数据数组中一个 32bit 宽字，因此**最大可随机索引 2MB 的数据数组**。当前主机 payload 上限是 262144 个 int = 1MB，只用了表容量的一半——留了一倍余量。

装载阶段本身也是一次访存（`size` 次顺序读下标数组），它发生在内核内部、被计入计时窗口，但只占总工作量的 \( 1/\text{NUM\_ITERATIONS} = 1/10000 \)，摊薄后可忽略（定量分析见 4.3.3）。

#### 4.2.3 源码精读

**片上下标表与装载循环** —— [ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_ubench.cpp:L13-L19](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_ubench.cpp#L13-L19)

```cpp
	int local_in0_index[524288];// max 2MB data indexing for 32-bitwidth port
	volatile INTERFACE_WIDTH temp_data_0;

	for (int i = 0; i < size; i++) {
	#pragma HLS PIPELINE II=1
		local_in0_index[i] = in0_index[i];
	}
```

注意两个循环的**刻意不对称**：

| | 装载循环（L16-L19） | 测量循环（L21-L25） |
| --- | --- | --- |
| 访问模式 | 顺序（`in0_index[i]`，i 递增） | 随机（`local_in0_index[j]` 做下标） |
| 流水指示 | `PIPELINE II=1`（显式加速） | 无（刻意串行） |
| 可否突发 | 可以，连续地址自动合并 | 不可以 |
| 目的 | 尽快把表搬进片上，别污染计时 | 让每次访问的延迟完整暴露 |

这个对比是本内核最值得记住的设计：**同一个内核里，该快的地方用流水，该慢的地方不用**——测量仪器不能改变被测对象的行为。

**配置头** —— [ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_config.h:L4-L7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/krnl_config.h#L4-L7)

```cpp
const int DWIDTH = 32;
#define INTERFACE_WIDTH ap_uint<DWIDTH>
const int WIDTH_FACTOR = DWIDTH/32;
const int NUM_ITERATIONS = 10000;
```

与带宽版唯一的实质差别是 `DWIDTH = 32`（带宽样板是 512）。`WIDTH_FACTOR=1` 意味着下标单位、int 个数、宽字个数三者重合，主机侧 `dataSize/WIDTH_FACTOR` 的换算在此退化为恒等——但代码保留了这层换算，使得改 `DWIDTH` 时主机无需动结构（见练习 3）。

#### 4.2.4 代码实践

**实践目标**：把 `524288` 这个 magic number 还原成推导，并评估它的安全边界。

**操作步骤**：

1. 纸面推导：\( 2\,\text{MB} = 2 \times 1024 \times 1024\,\text{B} \)，除以每字 4B，得 \( 524288 \)。
2. 检查当前 payload 上限：\( 262144 \times 4\,\text{B} = 1\,\text{MB} \le 2\,\text{MB} \)，安全余量 2 倍。
3. 思考实验（不要真改）：如果把主机 payload 循环上限改成 `payload <= 1048576`（即 4MB 数据），会发生什么？
4. 再推一步：如果把 `krnl_config.h` 的 `DWIDTH` 改成 512，同一张片上表能索引多大的数据数组？

**需要观察的现象 / 预期结果**：

- 步骤 3：\( 1048576 > 524288 \)，装载循环会往片上数组越界写入。HLS 综合的片上数组**没有边界检查**，越界是未定义行为——轻则覆盖相邻存储导致结果错乱，重则综合阶段直接报错。正确做法是同步把 `local_in0_index` 的容量扩到 \( 1048576 \)（代价是 4MB 片上存储，U200/U280 的 URAM 不一定放得下）。
- 步骤 4：`DWIDTH=512` 时 `WIDTH_FACTOR=16`，一个下标指向一个 64B 宽字，\( 524288 \times 64\,\text{B} = 32\,\text{MB} \)。同一张表的可索引范围随位宽线性放大，但「每次随机访问」取回的数据也从 4B 变成 64B——这正是 README 声称扫描「data port width」因素的实验语义：位宽变粗后，随机访问的粒度变粗，延迟特性（是否更容易触发行切换、控制器如何拆分）会随之改变。
- 本实践为纯推导型，无需硬件即可完成；步骤 3/4 的实际行为**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么不让内核直接写 `temp = in0[in0_index[j]]`，省掉片上表和装载循环？

**答案**：那样每次随机访问前要先从 DDR 读 `in0_index[j]`，虽然 `j` 递增使下标读取本身是顺序的，但**数据读的地址依赖于下标读的返回值**，两级访存只能串行——测到的平均延迟约等于「一次顺序读 + 一次随机读」之和，被下标访问污染。把表放进片上后，取下标只要 1 拍，暴露的就是纯随机访问延迟。此外下标端口走独立的 `bundle=gmem1`，与数据端口无争用。

**练习 2**：装载循环为什么可以放心地加 `PIPELINE II=1`？

**答案**：它是顺序读连续地址，相邻读无数据依赖（写的目标 `local_in0_index[i]` 每轮不同），流水化后读请求以每拍一个的速率发出并自动合并成突发，装载耗时约为 \( \text{size} \times \) 时钟周期量级。它只执行一次，而测量循环执行 `NUM_ITERATIONS` 轮，装载耗时占比约 \( 1/10000 \)，加速它对测量几乎无影响，不加速反而白白拉长计时窗口。

**练习 3**：保持内核代码不变，把 `DWIDTH` 从 32 改成 64，主机侧哪些量会自动适配、哪些需要人工核对？

**答案**：自动适配的部分：`WIDTH_FACTOR` 变为 2，`read_index` 长度变为 `dataSize/2`，setArg 的 `size` 也变为 `dataSize/2`，随机访问次数每轮减半。需要人工核对的部分：(a) 片上表容量虽然绰绰有余（\( 524288 \times 8\,\text{B} = 4\,\text{MB} \) 可索引范围远大于 1MB payload），但要确认片上资源够；(b) 本讲 4.3 的延迟公式分母用了 `payload/WIDTH_FACTOR`，会自动正确，但仓库**现有**的 GB/s 输出公式里 `payload*4` 的字节数没有乘 `WIDTH_FACTOR`，位宽一改就失真（这正是下一个模块要修的问题之一）。

### 4.3 延迟换算：从总时间到 ns/access

#### 4.3.1 概念说明

内核跑完后，主机手里只有一个 `kernel_time_in_sec`。要得到「平均每次随机访问延迟」，需要知道总访问次数：

\[
A_{\text{total}} \;=\; \text{NUM\_ITERATIONS} \times \underbrace{\frac{\text{payload}}{\text{WIDTH\_FACTOR}}}_{\text{size：每轮访问次数}}
\]

于是：

\[
\text{latency} \;=\; \frac{T}{A_{\text{total}}} \;\;\text{[秒/次]} \qquad
\text{latency}_{\text{ns}} \;=\; \frac{T \times 10^{9}}{\text{NUM\_ITERATIONS} \times \text{payload}/\text{WIDTH\_FACTOR}} \;\;\text{[ns/次]}
\]

本工程 `WIDTH_FACTOR=1`、`NUM_ITERATIONS=10000`，公式退化为 \( \text{latency}_{\text{ns}} = T \times 10^{9} / (10^{4} \times \text{payload}) \)。

#### 4.3.2 核心流程：现有输出到底是什么

仓库现在打印的这一行，变量名叫 `bw_result`、单位标 `GB/s`，却挂在 `Latency =` 标签后面——这是一个**口径误标**：

\[
\text{bw\_result} \;=\; \frac{\text{payload} \times 4 \times \underbrace{0.000010000}_{=\,\text{NUM\_ITERATIONS}/10^{9}}}{T} \times \text{NUM\_KERNEL} \;\;\text{[GB/s]}
\]

把魔数 \( 0.000010000 \) 还原成 \( 10^{4}/10^{9} \) 后可以看出，它就是「随机访问吞吐量」：总搬运字节 \( \text{payload} \times 4 \times 10^{4} \) 除以时间再换算成十进制 GB/s（与 u2-l3 推导的带宽公式同源）。

**两个口径之间可以互相换算**。每访问字节数 \( b = \text{WIDTH\_FACTOR} \times 4 \) 字节，于是：

\[
\text{latency}_{\text{ns}} \;=\; \frac{b \times 10^{9}}{\text{bw\_result} \times 10^{9}} \times 10^{0} \quad\Longleftrightarrow\quad \boxed{\;\text{latency}_{\text{ns}} = \frac{\text{WIDTH\_FACTOR} \times 4}{\text{bw\_result}}\;}
\]

（`bw_result` 以 GB/s 计。）本工程 `WIDTH_FACTOR=1`，即 `latency_ns = 4 / bw_result`。举例（**假设值，待本地验证**）：若某档打印 `bw_result = 0.02`，则平均延迟约 \( 4/0.02 = 200 \) ns。

顺带一提量级感：最大档 payload=262144 时总访问次数 \( 10^{4} \times 262144 \approx 2.6 \times 10^{9} \) 次，若平均延迟数百 ns，单档运行时间可达数百秒——延迟基准在大数组档上**非常耗时**，真机实验时要预留时间（或减小 `NUM_ITERATIONS`，但须同步改公式，见练习 3）。

#### 4.3.3 源码精读

**计时窗口** —— [ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp:L173-L187](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp#L173-L187)

```cpp
        // Start timer
        double kernel_time_in_sec = 0;
        std::chrono::duration<double> kernel_time(0);
        auto kernel_start = std::chrono::high_resolution_clock::now();

        //Setting the compute kernel arguments
        dataSize = dataSize / WIDTH_FACTOR;
		for (int i = 0; i < NUM_KERNEL; i++) {
            OCL_CHECK(err, err = cmpt_krnl[i].setArg(0, source_in_buffer[i]));
            OCL_CHECK(err, err = cmpt_krnl[i].setArg(1, index_in_buffer[i])); 
            OCL_CHECK(err, err = cmpt_krnl[i].setArg(2, dataSize)); 
            OCL_CHECK(err, err = q.enqueueTask(cmpt_krnl[i]));
		}
        q.finish();
```

- `dataSize = dataSize / WIDTH_FACTOR;`（L179）把单位从 int 个数换算成宽字个数再 setArg——这就是内核里的 `size`。**注意这行会覆写 `dataSize`**，所以后面公式只能用循环变量 `payload`（它没被动过）。
- 计时窗口与带宽版一样是「主机侧粗窗口」：包含 `setArg`、`enqueueTask` 的启动路径、`finish` 的返回延迟，以及内核内部的**下标装载循环**。对延迟测量的影响分层来看：
  - 装载循环：`size` 次顺序读，仅占总访问量的 \( 1/10^{4} \)，摊薄后可忽略；
  - 启动开销：通常几微秒到几十微秒量级，对总时间以百毫秒计的大 payload 档可忽略；但**对小 payload 档是系统性高估**——例如 payload=16 时纯访问时间只有 \( 1.6\times10^{5} \times 200\,\text{ns} = 32\,\text{ms} \)（假设值），启动开销占比仍然很小，但 payload 更小的实验改法会放大这一误差。改进方向是 u2-l3 讲过的 `cl_event` 内核级计时（队列已开 `CL_QUEUE_PROFILING_ENABLE`，见 L56-L60）。

**误标的结果输出** —— [ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp:L189-L195](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp#L189-L195)

```cpp
        // Stop timer
        auto kernel_end = std::chrono::high_resolution_clock::now();
        kernel_time = std::chrono::duration<double>(kernel_end - kernel_start);
        kernel_time_in_sec = kernel_time.count();
        std::cout << "Execution time = " << kernel_time_in_sec << std::endl;
		double bw_result = payload * 4 * 0.000010000 / kernel_time_in_sec * NUM_KERNEL;
        std::cout << "Payload Size: " << payload*4/(1024.0*1024.0) << "MB - Latency = " << bw_result << "GB/s"<< std::endl;
```

逐项拆解 `bw_result = payload * 4 * 0.000010000 / kernel_time_in_sec * NUM_KERNEL`：

| 因子 | 含义 | 备注 |
| --- | --- | --- |
| `payload * 4` | 每轮访问的总字节（int×4B） | 未乘 `WIDTH_FACTOR`，改位宽后会失真 |
| `0.000010000` | \( \text{NUM\_ITERATIONS}/10^{9} \) | 魔数，改 `NUM_ITERATIONS` 时**必须手改** |
| `/ kernel_time_in_sec` | 除以秒 | 得到 B/s |
| `* NUM_KERNEL` | 乘内核数 | 本工程恒为 1（L15），乘了也无害 |

打印行的标签 `Latency = ... GB/s` 把带宽口径的数挂上了延迟的名字——数值没错（作为随机访问吞吐），**单位与名称错了**。下一个实践就把它修正成真正的 ns/access。

**README 的滞后片段** —— [ubench/offchip_latency/datacenter/README.md:L11-L15](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/README.md#L11-L15)

```markdown
2. **Random Access Data Array**
   To change the random access data array size, update the 'payload' variable in the host code (**host.cpp**). Currently, 'payload' varies from 256 (1KB) to 262144 (1MB) ...
      for (int payload(256); payload <= 262144; payload*=2){
```

README 的示例代码写 payload 从 256 起，而真实代码（host.cpp:L99）从 16 起；README 正文第 3 行写的 "from 64B to 1MB" 反而与代码一致（\( 16 \times 4\,\text{B} = 64\,\text{B} \)）。另外 README 第 6 行说改位宽要编辑 `krnl_ubench.h`，实际文件名是 `krnl_config.h`。两条都是文档滞后，**以 `src/` 代码为准**——这与 u1-l4、u3-l2 反复强调的阅读纪律一致。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：把 `host.cpp` 的输出从误标的 `GB/s` 改成真正的平均随机访问延迟 `ns/access`，并用旧输出做交叉验证。

**操作步骤**：

1. 打开 [host.cpp:L194-L195](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/src/host.cpp#L194-L195)，用下面两行替换原有的 `bw_result` 计算与打印（**示例代码**）：

   ```cpp
   int accesses_per_iter = payload / WIDTH_FACTOR;   // 每轮外层的随机访问次数
   double latency_ns = kernel_time_in_sec /
                       (NUM_ITERATIONS * accesses_per_iter) * 1e9;
   std::cout << "Random array size: " << payload * 4 / 1024.0 << "KB - Avg latency = "
             << latency_ns << " ns/access" << std::endl;
   ```

2. 检查公式用到的三个量从哪来：
   - `payload` 是循环变量（**未被** L179 的 `dataSize/WIDTH_FACTOR` 覆写污染，这就是不用 `dataSize` 的原因）；
   - `NUM_ITERATIONS` 来自 `krnl_config.h`，主机 include 了同一个头（host.cpp:L13），内核循环次数与公式分母**自动同步**——比带宽版藏在 `0.000010000` 里的魔数写法更安全；
   - `WIDTH_FACTOR` 同样来自契约头。
3. 若本机有 Vitis 环境：`make exe TARGET=sw_emu`（或 `make check`）先确认编译通过、程序能跑完 15 档（注意 sw_emu 下 dataSize 被钉为 256，只验证功能，数值无物理意义）。
4. 交叉验证（纸面）：保留旧的一行打印（或者临时并排输出），核对 `latency_ns × bw_result ≈ WIDTH_FACTOR × 4 = 4`。

**需要观察的现象**：

- 每一档输出形如 `Random array size: 64KB - Avg latency = xxx ns/access`。
- （有真机时）随数组从 64B 涨到 1MB，延迟应当呈**缓慢上升**趋势：数组越大，随机地址在 DDR 行/ bank 间跳转越分散，open-page 命中率越低。

**预期结果**：

- 代数关系 `latency_ns ≈ 4 / bw_result`（本工程 `WIDTH_FACTOR=1`）在每个 payload 档都成立，误差只来自浮点舍入——这同时验证了新公式与旧实现是同一份数据的两种口径。
- 真机上的延迟绝对量级预期在数百 ns（Alveo DDR4 随机访问的常见量级），具体数值**待本地验证**。
- 若在 sw_emu 下运行，输出的 ns 数值没有物理意义（u1-l3 的结论：仿真只验证功能链路）。

#### 4.3.5 小练习与答案

**练习 1**：推导 `latency_ns = (WIDTH_FACTOR × 4) / bw_result`，并说明为什么 `WIDTH_FACTOR` 出现在分子。

**答案**：`bw_result`（GB/s）= 总字节/总时间/10⁹，其中总字节 = \( \text{payload} \times 4 \times \text{NUM\_ITERATIONS} \)；而每次访问搬 \( \text{WIDTH\_FACTOR} \times 4 \) 字节、总访问 \( \text{payload}/\text{WIDTH\_FACTOR} \times \text{NUM\_ITERATIONS} \) 次，两者相除正好差因子 \( \text{WIDTH\_FACTOR} \times 4 \)（每访问字节）。于是 latency（s）= 每访问字节 ÷ 字节速率 = \( \text{WIDTH\_FACTOR} \times 4 / (\text{bw\_result} \times 10^{9}) \)，乘 \( 10^{9} \) 折成 ns 即得。`WIDTH_FACTOR` 在分子是因为一次「访问」取的是一个宽字（`WIDTH_FACTOR` 个 int），位宽越宽、单次访问搬运的字节越多。

**练习 2**：如果把 `NUM_ITERATIONS` 从 10000 改成 1000，需要同步改哪些地方？

**答案**：内核的测量外层循环（krnl_ubench.cpp:L21）用的是 `krnl_config.h` 里的 `NUM_ITERATIONS`，改头文件即同时作用于内核与主机。采用 4.3.4 的新公式后无需再改别的（公式直接引用 `NUM_ITERATIONS`）；但若保留旧输出行，其中的魔数 `0.000010000` 是手写的 \( 10^{4}/10^{9} \)，必须改成 `0.000001000`，否则吞吐报表静默失真 10 倍——这正是魔数写法的危害，也是本实践改写公式的动机之一。另外总访问次数降为 1/10，小 payload 档的计时窗口变短，启动开销占比上升，测量误差增大。

**练习 3**：为什么延迟微基准只部署 1 个内核、1 个数据端口，而带宽微基准要多端口并发？

**答案**：被测量决定了部署形态。带宽测的是「并发通路聚合的极限」，要多端口/多实例把内存系统压满；延迟测的是「单条依赖链上一次访问的端到端时间」，任何并发都会引入控制器排队与互连争用，让测到的「延迟」变成「排队延迟 + 访问延迟」的混合量。单内核单端口把并发因素隔离出去，`ubench.ini` 里 `nk=krnl_ubench:1` 与主机 `NUM_KERNEL=1`（host.cpp:L15）正是这个意图。

## 5. 综合实践

**任务：把延迟微基准改造成一台输出规范、可交叉验证的「延迟扫描仪」。**

在你自己复制出的实验目录中（不要直接改仓库源码，复制 `DDR/32bit_per_access` 整个目录即可）完成三步：

1. **修正输出口径**：按 4.3.4 把 `GB/s` 误标输出替换为 `ns/access`，同时保留一行原始吞吐输出，运行后核对两口径满足 `latency_ns × bw_result ≈ WIDTH_FACTOR × 4`。
2. **补一条换算说明**：在打印里同时给出「随机数组大小（KB）」和「总访问次数」（\( \text{NUM\_ITERATIONS} \times \text{payload}/\text{WIDTH\_FACTOR} \)），并据此估算最大档的运行时长（假设平均延迟 200 ns 做量级估算，标注为假设）。
3. **设计对照实验**（纸面 + 可选真机）：
   - 把 `DWIDTH` 改为 512（同步按 4.2.4 核对片上表容量与 `read_index` 长度），预测延迟会怎么变、公式里哪些量自动适配；
   - 把 `ubench.ini` 的两行 `sp` 从 `DDR[0]` 换成 `HBM[0]`（对照 `HBM/32bit_per_access/ubench.ini`），并说明为什么这一步不需要动内核源码；
   - 若有真机，跑 DDR 与 HBM 两版，绘制「延迟 vs 数组大小」曲线，解释两条曲线的差异来源（HBM 伪通道更窄、单通道容量更小，随机访问的 bank/row 行为不同）。

**验收标准**：能说清每一次修改触发了哪些文件的联动（契约头 / 内核 / 主机 / ini），能从打印值反推出平均延迟，并能指出仓库原版输出中标签与单位误标的位置。无硬件环境下，第 3 步以预测 + 依据的形式完成，全部标注**待本地验证**。

## 6. 本讲小结

- 延迟测量的核心手法是**放大再平均**：洗牌下标表制造随机排列 → 内核按表串行访问 → 总时间除以 `NUM_ITERATIONS × size` 得平均延迟。
- **两个循环刻意不对称**：下标装载循环顺序、可突发、`PIPELINE II=1` 加速；测量循环随机、不可突发、不加流水指示以保住串行依赖链——测量仪器不能改变被测对象的行为。
- 下标表必须**先搬进片上**（`local_in0_index[524288]`，即 2MB/4B 的换算），否则每次随机访问前还要串一次 DDR 下标读取，延迟被污染。
- 延迟公式：\( \text{latency}_{\text{ns}} = T \times 10^{9} / (\text{NUM\_ITERATIONS} \times \text{payload}/\text{WIDTH\_FACTOR}) \)；与仓库现有 `bw_result` 的换算关系是 \( \text{latency}_{\text{ns}} = \text{WIDTH\_FACTOR} \times 4 / \text{bw\_result} \)。
- 仓库现有输出把随机访问吞吐标成了 `Latency = ... GB/s`，属口径误标；`max_read_burst_length`/`num_read_outstanding` 对随机访问不生效，是模板遗留。
- 文件一致性两处坑：README 的 payload 起点片段（写 256，实际 16）与位宽文件名（写 `krnl_ubench.h`，实际 `krnl_config.h`）均滞后，以 `src/` 为准；DDR 与 HBM 两版内核逐字节等价（仅行尾空格差异），内存类型差异全在 `ubench.ini` 的 `sp` 行。

## 7. 下一步学习建议

- **u4-l3（嵌入式平台移植）**：看同一微基准到 ZCU104 上的形态——独立 `host.h`、`my_timer.h` 计时、`run_app.sh` 板上运行、`xrt.ini` 打开 profile，体会嵌入式与数据中心两条工程化路线的差异。
- **u2-l3 / u7-l3（测量方法学批判）**：本讲的计时窗口仍含 `setArg` 与启动开销，建议带着「用 `cl_event` 时间戳剥离启动开销」的目标重读 u2-l3 的双口径计时一节，并在 u7-l3 中系统化误差分析。
- **源码延伸阅读**：对照 `ubench/offchip_latency/datacenter/HBM/32bit_per_access/`（`ubench.ini` 与 bank flag 的另一种写法）与 `ubench/offchip_latency/embedded/32bit_per_access/`（`ls` 已确认存在，嵌入式版结构将在 u4-l3 展开）。
- **向案例研究过渡**：学完 u4 全部三讲后，进入 u6 的 KNN/SpMV 案例——那里的 load 级双缓冲设计正是「带宽优先」与「延迟优先」两种访存模式的工程化组合。
