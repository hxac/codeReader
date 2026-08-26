# 读带宽内核逐行精读：双端口读循环与防优化技巧

## 1. 本讲目标

本讲在 u2-l1（HLS 内核基础）与 u2-l2（主机编程模型）之上，把读带宽微基准的内核 `krnl_ubench.cpp` 逐行读透。学完本讲你应该能够：

1. 逐行解释两个读循环如何各自独占一个 `gmem` bundle 端口，并在 `DATAFLOW` 下并发驱动双端口；
2. 说明 `volatile` 的「双保险」写法（指针 volatile + 临时变量 volatile）如何防止读操作被编译器当作死代码删除，以及 `NUM_ITERATIONS = 10000` 如何把执行时间放大到主机计时器可分辨的量级；
3. 用 `git diff` 亲自验证：DDR 版与 HBM 版的**内核源码逐字节相同**，内存类型的全部差异都落在 `ubench.ini` 连接配置与主机 bank 绑定上。

## 2. 前置知识

本讲默认你已读过 u2-l1 与 u2-l2（`m_axi`/`s_axilite`、`bundle`、`ap_uint`、`DATAFLOW`/`PIPELINE`、`cl_mem_ext_ptr_t` 等）。这里补充三个本讲要深挖的背景概念：

- **AXI 突发传输（burst）**：AXI 总线一次读事务不是只搬一个数据，而是先发一个起始地址，随后连续传 N 个数据拍（beat）。`max_read_burst_length=16` 表示单次突发最多 16 拍；本工程每拍 512 bit（64 字节），所以单次突发最多搬 \( 16 \times 64\,\text{B} = 1024\,\text{B} \)。突发越长，地址/握手开销被摊得越薄——这正是五因素中的「最大突发长度」。
- **volatile 与死代码消除（DCE）**：C++ 编译器只需保证「可观测行为」不变。一个只读不用的变量，读取它对程序结果没有任何影响，编译器有权整段删掉。FPGA 的 HLS 工具做的是 C++ 到 RTL 的综合，同样会做这类优化。`volatile` 声明「该对象的每次访问本身就是可观测副作用」，从而封死删除的口子。
- **HLS 数据流进程模型**：`#pragma HLS DATAFLOW` 提示综合器把区域内的代码块（这里是两个循环）当作**并发进程**重叠执行，而不必等前一个循环彻底结束。前提是各块之间没有依赖；本讲的两个循环各写各的临时变量、各读各的端口，恰好满足前提。

另外回顾一个本讲反复用到的换算（u1-l4 已建立）：`krnl_config.h` 中 `DWIDTH = 512`，`WIDTH_FACTOR = DWIDTH/32 = 16`，内核收到的 `size` 参数单位是 **512-bit 宽字的个数**，主机送参前要先把 int 个数除以 `WIDTH_FACTOR`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp` | DDR 版读带宽 HLS 内核（全讲主角） | 双读循环、pragma、volatile |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h` | 内核与主机共用的参数契约头 | `NUM_ITERATIONS`、`WIDTH_FACTOR` |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp` | OpenCL 主机程序 | `NUM_PORT`、setArg 顺序、带宽公式 |
| `ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/krnl_ubench.cpp` | HBM 版内核 | 与 DDR 版做 diff 对照 |
| `ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/host.cpp` | HBM 版主机程序 | 与 DDR 版唯一的语义差异（bank flag） |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini` | DDR 版链接期连接配置 | `sp` 端口→DDR bank 映射 |
| `ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/ubench.ini` | HBM 版链接期连接配置 | `sp` 端口→HBM bank 映射 |

## 4. 核心概念与源码讲解

### 4.1 双端口读循环：一个内核，两台并发「读引擎」

#### 4.1.1 概念说明

读带宽微基准要测的是「并发端口数 × 端口位宽」能压出多少带宽，所以内核里必须有**多条互相独立的读数据通路**。本工程用「一个内核函数 + 两个指针参数 + 两个不同 bundle 的 `m_axi` 端口 + 两份互不依赖的循环」来实现：每个循环就是一台独立的读引擎，各自把自己端口上的 `size` 个宽字从头读到尾，重复 `NUM_ITERATIONS` 遍。

为什么强调「不同 bundle」？u2-l1 讲过：**bundle 名是端口分组键**，`gmem0` 与 `gmem1` 异名，所以 `in0`、`in1` 各自综合成一个独立的 AXI master 端口；若两者同名，两个循环就会挤同一个端口、共享其带宽，微基准就测不出「并发端口数」这个因素了。

为什么强调「两份循环、两个临时变量」？`DATAFLOW` 只对**互不依赖**的代码块生效。两个循环分别只写 `temp_data_0` 与 `temp_data_1`、分别只读 `in0` 与 `in1`，零依赖，才能被当作两个并发进程重叠执行。这也解释了一个看似多余的细节：为什么不用一个 `temp` 变量让两个循环轮流写——那样会引入假依赖，破坏并发。

#### 4.1.2 核心流程

内核被主机启动后，两条读引擎并发执行，每条引擎的执行模式如下：

```text
对 i = 0 .. NUM_ITERATIONS-1:            # 外层：重复 10000 遍，放大时长
    对 j = 0 .. size-1:                   # 内层：连续地址扫描（II=1 流水线）
        temp_data_x = inX[j]              # 发出一个 512-bit 读请求并接住数据
```

- 内层循环的 `j` 每次加 1，地址每次前进 64 字节，**严格连续**。HLS 会把连续的读自动合并成 AXI 突发：每 `max_read_burst_length=16` 拍（即 1024 B）合为一次突发事务。
- `PIPELINE II=1` 让内层循环每时钟拍发出一个宽字请求。在 300 MHz 下单端口理论峰值 \( 300\times10^{6} \times 64\,\text{B} = 19.2\,\text{GB/s} \)，双端口 \( 38.4\,\text{GB/s} \)——这正是 u2-l1 推出的本工程理论峰值。
- 外层 `i` 循环每遍都重读同一段地址：FPGA 的 `m_axi` 读通路上**没有 cache**，每次读都会到达内存控制器，所以重复读不污染测量（这点与 CPU 微基准必须清缓存截然不同，详见 4.2）。

一次主机调用的完整数据流：

```text
host: setArg(0, buf_in0) / setArg(1, buf_in1) / setArg(2, size)
        ↓ enqueueTask
内核: DATAFLOW ──┬── 引擎0: in0 上读 NUM_ITERATIONS × size 个宽字 → temp_data_0
                └── 引擎1: in1 上读 NUM_ITERATIONS × size 个宽字 → temp_data_1
        ↓ return
host: finish() 返回 → 用计时窗口换算带宽
```

#### 4.1.3 源码精读

先看内核签名。`in0`、`in1` 是两个 volatile 宽字指针，`size` 是宽字个数：

- [read/DDR/2ports_512bit/src/krnl_ubench.cpp:4-6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L4-L6)
  这三行定义了内核签名与两个内存端口：`in0` 挂到 `bundle=gmem0`、`in1` 挂到 `bundle=gmem1`，两个异名 bundle 生成两个独立 AXI master；`offset=slave` 表示基地址由主机运行时写入（u2-l1 已讲）；`max_read_burst_length=16` 把单次读突发限制为 16 拍（1024 B）。

- [read/DDR/2ports_512bit/src/krnl_ubench.cpp:8-12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L8-L12)
  把 `in0`、`in1`、`size`、`return` 全部映射到 `control` 这个 AXI-Lite 寄存器组，对应主机的 `setArg`（`size` 占一个 32 位寄存器，主机端 `dataSize/WIDTH_FACTOR` 的结果写进这里）。

- [read/DDR/2ports_512bit/src/krnl_ubench.cpp:14-17](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L14-L17)
  声明两个 **volatile** 临时变量（4.2 的主角），随后 `#pragma HLS DATAFLOW` 开启数据流区域——区域内的两个循环将被综合为并发进程。

- [read/DDR/2ports_512bit/src/krnl_ubench.cpp:19-24](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L19-L24)
  引擎 0：外层重复 `NUM_ITERATIONS` 遍，内层以 `PIPELINE II=1` 连续读 `in0[j]` 存入 `temp_data_0`。`j` 连续递增 → 地址连续 → 自动合并为 16 拍突发。

- [read/DDR/2ports_512bit/src/krnl_ubench.cpp:26-31](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L26-L31)
  引擎 1：与引擎 0 结构完全对称，只是读 `in1`、写 `temp_data_1`。注意它是**与引擎 0 并列的第二份循环**，而不是塞进同一个循环体里——这是让两个端口真正并行的结构保证。

再看契约头里的两个常量如何被这两份循环消费：

- [read/DDR/2ports_512bit/src/krnl_config.h:4-7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L4-L7)
  `DWIDTH=512` 派生出 `INTERFACE_WIDTH`（即 `ap_uint<512>`）与 `WIDTH_FACTOR=16`；`NUM_ITERATIONS=10000` 是外层循环次数，同时它就是主机带宽公式里魔数 `0.000010000` 的来源（见 4.2.3）。

主机侧与双端口对应的代码（为 4.3 对照做铺垫，细节 u2-l2 已讲）：

- [read/DDR/2ports_512bit/src/host.cpp:15-16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L15-L16)
  `NUM_KERNEL=1`、`NUM_PORT=2`：内核实例数 × 每实例端口数。**这份主机代码对 NUM_PORT 是泛型的**——所有缓冲向量按 `NUM_KERNEL*NUM_PORT` 定长，setArg 按循环走，改端口数只需改这一个宏（综合实践会用到）。

- [read/DDR/2ports_512bit/src/host.cpp:155-164](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L155-L164)
  先 `dataSize/WIDTH_FACTOR` 换算成宽字个数，再按 `setArg(0..NUM_PORT-1, buffer)`、`setArg(NUM_PORT, size)` 的顺序绑参——**参数下标顺序必须与内核签名中指针参数、size 参数的排列顺序一致**，然后 `enqueueTask` 异步启动。`DATAFLOW` 让内核内两台引擎并发，主机只 enqueue 一次。

#### 4.1.4 代码实践：纸上推演请求流

**实践目标**：不开 Vitis、不占真机，只靠笔算验证你真的理解了「size 的单位、突发合并、时间放大」三件事。

**操作步骤**：

1. 设主机 payload 档位为 4096（单位：int）。计算 `size`（宽字个数）＝ 4096 / 16。
2. 计算单端口一次外层迭代发出的 beat 总数与突发事务数（每突发 16 拍）。
3. 计算单端口 `NUM_ITERATIONS` 遍的总 beat 数，并按 300 MHz、II=1 估算纯执行时间。
4. 对照 [read/DDR/2ports_512bit/src/host.cpp:172](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L172) 的带宽公式，把你的数字代进去核算是否自洽。

**需要观察的现象 / 预期结果**：

- `size = 256` 个 512-bit 宽字；每外层迭代每端口 256 拍 ＝ 16 次突发 × 1024 B ＝ 16 KB（恰等于 4096 int × 4 B，自洽）。
- 单端口总 beat ＝ \( 10000 \times 256 = 2{,}560{,}000 \) 拍；理想执行时间 \( \frac{2.56\times10^{6}}{300\times10^{6}\,\text{Hz}} \approx 8.53\,\text{ms} \)。
- 双端口理想总流量 \( 2 \times 2.56\times10^6 \times 64\,\text{B} = 327.68\,\text{MB} \)，除以 8.53 ms 得 38.4 GB/s，与理论峰值一致——公式、位宽、端口数三者互相咬合。

本实践为纯笔算，结果可直接自查；无需本地验证。

#### 4.1.5 小练习与答案

**练习 1**：若把第二个循环的 pragma 改成 `bundle=gmem0`（与第一个同名），会发生什么？

**答案**：`in0`、`in1` 会被综合到**同一个** AXI master 端口上，两个循环共享该端口带宽并相互仲裁，微基准就测不出「并发端口数」的作用；同时主机侧两个缓冲都必须绑到这一个端口可达的 bank。bundle 名是决定端口数量的关键开关。

**练习 2**：payload = 262144（int）时，每端口一次外层迭代发出多少次突发事务？

**答案**：`size = 262144/16 = 16384` 个宽字；每次突发 16 拍 → \( 16384/16 = 1024 \) 次突发，每次突发 1024 B，共 1 MB。

**练习 3**：内核参数 `size` 的单位是什么？主机为什么送参前要除以 `WIDTH_FACTOR`？

**答案**：单位是 512-bit 宽字（`INTERFACE_WIDTH` 元素）个数。主机缓冲区按 int（32 bit）分配，内核端口一次搬 16 个 int，所以要把 int 个数除以 `WIDTH_FACTOR=16` 换算成宽字个数，内层循环 `j < size` 才能覆盖全部数据。

### 4.2 防优化技巧：volatile 双保险与 NUM_ITERATIONS 时间放大

#### 4.2.1 概念说明

这个内核做的是「纯粹的读、读完成就丢」。这在 C++ 语义上是**零可观测效果**的代码：`temp_data_0` 从未被使用，`in0[j]` 的值没人消费。编译器的「as-if」规则允许它认为整个循环什么都没干而直接删除——真被删掉，带宽测量就归零了。uBench 用两招封死这条路：

1. **volatile 指针**：`volatile INTERFACE_WIDTH* in0` 声明「经此指针的每次读访问都是可观测副作用」。于是每个 `in0[j]` 都必须真实发出、按程序顺序执行，不能合并、不能省略。这是保证「每个宽字读请求都发生」的第一道保险。
2. **volatile 临时变量**：`volatile INTERFACE_WIDTH temp_data_0` 让「把读到的值写进 temp」也成为可观测副作用。于是每次加载的结果都被一次 volatile store 消费，加载与存储形成一一配对，进一步堵死编译器做加载转发、合并或省略的口子。这是第二道保险。

两道保险分工不同：指针 volatile 保证**读请求发出**，temp volatile 保证**读到的值必须被消费**。任意去掉一道，另一道理论上仍能兜住大部分优化，但两道一起去掉，循环体就彻底无副作用，可被整体删除。

第二招是**时间放大**：`NUM_ITERATIONS = 10000` 把同一 `size` 个宽字的扫描重复一万遍。动机在 u2-l3 已分析过——主机用 `std::chrono` 计时，窗口里混有内核启动开销；把纯执行时间放大到数百毫秒量级，启动开销（毫秒级）的相对影响就被压到可忽略（小 payload 档除外）。同时它把总流量放大一万倍，带宽公式的分子里因此藏着 `NUM_ITERATIONS`。

#### 4.2.2 核心流程

防优化与放大机制在循环结构中的落位：

```text
volatile INTERFACE_WIDTH* inX        ← 保险①：每次 inX[j] 读取都是副作用，必须执行
volatile INTERFACE_WIDTH temp_data_X ← 保险②：读取结果必须被 volatile 写消费

for i in 0..NUM_ITERATIONS-1:       ← 放大器：同一扫描重复 10000 遍
    for j in 0..size-1:             ← II=1：每拍一个宽字请求
        temp_data_X = inX[j]        ← 副作用写 ← 加载（配对，不可拆）
```

总读请求量与带宽公式的自洽校验（单端口）：

\[
\text{总字节数} = \text{NUM\_ITERATIONS} \times \text{size} \times \frac{\text{DWIDTH}}{8}
= 10000 \times \frac{\text{payload}}{16} \times 64\,\text{B}
= \text{payload} \times 4 \times 10000
\]

即主机公式 `payload * 4 * 0.000010000 / kernel_time_in_sec * NUM_KERNEL * NUM_PORT` 中，`payload*4` 是单端口单遍字节数、`0.000010000 = NUM_ITERATIONS/10^9` 把总字节数折成 GB/s——魔数正是 `NUM_ITERATIONS` 的化身。

#### 4.2.3 源码精读

- [read/DDR/2ports_512bit/src/krnl_ubench.cpp:14-15](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L14-L15)
  两个 volatile 临时变量在 `DATAFLOW` 区域之前声明。它们同时服务于两个目的：一是本模块的防优化保险②；二是「每循环独占一个 temp」满足 dataflow 的无依赖前提（4.1.1）。

- [read/DDR/2ports_512bit/src/krnl_ubench.cpp:21-22](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L21-L22)
  `PIPELINE II=1` + `temp_data_0 = in0[j]`：一行代码里完成一次 volatile 加载与一次 volatile 存储。II=1 要求这对「加载→存储」每拍完成一次，流水线不因副作用而停顿——这就是每端口每拍一个 512-bit 请求的由来。

- [read/DDR/2ports_512bit/src/krnl_config.h:7](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L7)
  `const int NUM_ITERATIONS = 10000;` 单点定义放大倍数。它在内核端控制外层循环（[krnl_ubench.cpp:19](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L19) 与 [krnl_ubench.cpp:26](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L26)），在主机端以 `0.000010000` 的身份进入带宽公式——**改这个常量必须同步改公式魔数**，这是契约头里隐藏最深的联动点。

- [read/DDR/2ports_512bit/src/host.cpp:172](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L172)
  带宽公式行。把 4.2.2 的推导与这行对照：`payload*4`（单端口单遍字节）× `0.000010000`（= 10000/10⁹，把重复次数折进分子并换算 GB）÷ 秒 × `NUM_KERNEL*NUM_PORT`（所有端口并发）＝ GB/s。

顺带回答一个常见疑问：**为什么重复读同一万个数据不算「作弊」？** 因为 FPGA 的 `m_axi` 读通路没有 cache，`in0[j]` 每次都会作为 AXI 事务发到内存控制器；微基准要防御的是**编译期**的删除，而不是 CPU 上那种**运行期**缓存命中。

#### 4.2.4 代码实践：三种「去 volatile」思想实验

**实践目标**：通过推演（而非改码编译）搞清楚两道 volatile 保险各自守哪扇门。

**操作步骤**：

1. 情形 A：把第 4 行两个指针的 `volatile` 都去掉、保留 temp 的 volatile。推演加载能否被删除？能否被合并/重排？
2. 情形 B：保留指针 `volatile`、去掉第 14-15 行 temp 的 `volatile`。推演读请求是否仍然全部发出？
3. 情形 C：两处 `volatile` 全去掉。推演综合结果。
4. 有条件的话（本机装有 Vitis HLS）：任选一情形综合，对比综合报告中的 AXI 端口个数与「burst 数/循环展开」信息。

**需要观察的现象 / 预期结果**：

- 情形 A：加载结果仍被 volatile store 消费，加载**不会**被删除；但非 volatile 加载允许合并、重排，II 与突发行为可能改变。
- 情形 B：指针 volatile 保证每个 `in0[j]` 都必须执行，**读请求照常全部发出**，测量基本仍有效；temp 的值虽没人用，但加载已由指针侧兜底。
- 情形 C：循环体对可观测状态零影响，整个内层循环可被删除，测得带宽将失真甚至归零。
- 步骤 4 的综合报告对比：**待本地验证**（需要 Vitis 2020.2 环境；本讲义未实际运行）。

#### 4.2.5 小练习与答案

**练习 1**：主机公式里的 `0.000010000` 与 `krnl_config.h` 中哪个常量相关？改常量时要联动什么？

**答案**：与 `NUM_ITERATIONS = 10000` 相关，`0.000010000 = 10000/10^9`（同时隐含「带宽按十进制 GB/s 计」）。若把 `NUM_ITERATIONS` 改成别的值，必须同步改公式中的这个魔数，否则带宽刻度整体偏移。

**练习 2**：在 CPU 上做内存带宽微基准通常要每次换数据或清缓存，为什么这个内核可以放心地重复读同一段数据一万遍？

**答案**：CPU 有 cache，重复读会命中缓存测不到内存；FPGA HLS 的 `m_axi` 读通路没有 cache，每次读都变成 AXI 事务发往内存控制器。要防的是编译器在综合期把「无用的读」删掉，所以用 volatile 而不是换数据。

**练习 3**：payload = 256（最小档）时，300 MHz、II=1 下纯执行时间约多少？这说明了什么？

**答案**：`size = 256/16 = 16` 拍，总 \( 10000 \times 16 = 160{,}000 \) 拍 ≈ \( 160000/300\text{MHz} \approx 0.53\,\text{ms} \)。这与内核启动开销同一量级，印证 u2-l3 的结论：最小档的主机计时被启动开销严重污染，读出的带宽偏低。

### 4.3 DDR 与 HBM 版本对照：同一个内核，两套连接

#### 4.3.1 概念说明

uBench 把「内存类型」设计成**内核之外**的属性：DDR 与 HBM 两个工程目录里的 HLS 内核**逐字节相同**（4.3.3 用 diff 验证），差异全部落在两处——

1. **链接期**：`v++ -l` 阶段读的 `ubench.ini`，用 `sp=` 行决定每个端口接到哪个内存通道（`DDR[1]` 或 `HBM[0]`），用 `slr=` 决定内核放在哪个 SLR；
2. **运行期**：主机用 `cl_mem_ext_ptr_t.flags` 把缓冲区放进对应通道——DDR 版写 `XCL_MEM_DDR_BANK1` 宏，HBM 版写 `bank[0]`（即 `0 | XCL_MEM_TOPOLOGY` 的拓扑式编号，u2-l2 已讲）。

这个「内核内存无关（memory-agnostic）、连接配置分层」的设计正是微基准能廉价扫描 `MEMORY_TYPE` 维度的原因：换一种内存不需要重写内核，只改 ini 与主机 flag。代价是两处必须**手工对齐**——`sp` 行的端口名/bank 与主机 flag 的 bank 必须一一对应，错位则数据通路不通或读写错误的 bank（u1-l4 已总结）。

另一个值得注意的观察：两个工程的 `sp` 行都把**两个端口接到同一个通道**（DDR 版两个端口都连 `DDR[1]`，HBM 版都连 `HBM[0]`）。所以这个手写工程测的其实是「双端口共享单通道」的争用行为，而不是「各占一通道」的聚合峰值——这是读数据时容易误读的点。

#### 4.3.2 核心流程

从「同一份内核源码」到「两块不同的内存」的分流过程：

```text
krnl_ubench.cpp（DDR 版 ≡ HBM 版，逐字节相同）
        │
        ├── v++ -c：综合出 2 个 AXI master（gmem0/gmem1）——与内存类型无关
        │
        ├── v++ -l --config ubench.ini：链接期分流
        │       DDR 版: slr=krnl_ubench_1:SLR1; sp=in0:DDR[1]; sp=in1:DDR[1]
        │       HBM 版: slr=krnl_ubench_1:SLR0; sp=in0:HBM[0]; sp=in1:HBM[0]
        │
        └── host.cpp 运行期绑定：DDR 版 flags=XCL_MEM_DDR_BANK1；HBM 版 flags=bank[0]
```

#### 4.3.3 源码精读

先看两份 ini 的对照：

- [read/DDR/2ports_512bit/ubench.ini:2-6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L2-L6)
  DDR 版连接配置：内核实例 `krnl_ubench_1` 放在 SLR1；`in0`、`in1` 两个端口都连到 `DDR[1]`（U200 的 2 通道 DDR4 之一）；`nk=krnl_ubench:1` 只例化一个内核。

- [read/HBM/2ports_512bit/ubench.ini:2-6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/ubench.ini#L2-L6)
  HBM 版连接配置：同一份内核，但实例放 SLR0，两个端口都连 `HBM[0]`（U280 的 32 伪通道 HBM 之一）。与 DDR 版逐行比较，差异只有 SLR 编号与内存名。

再看主机端唯一的语义差异：

- [read/DDR/2ports_512bit/src/host.cpp:115-128](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L115-L128)
  DDR 版 bank 绑定：所有端口的缓冲 `flags = XCL_MEM_DDR_BANK1`，与 ini 的 `sp=...DDR[1]` 对齐。（u2-l2 已指出：`is_emulation()` 与 `else` 两个分支内容完全相同，是模板遗留的空骨架。）

- [read/HBM/2ports_512bit/src/host.cpp:115-128](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/host.cpp#L115-L128)
  HBM 版同一位置：`flags = bank[0]`。`bank` 表在文件顶部（[host.cpp:19-28](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/host.cpp#L19-L28)）用 `BANK_NAME(n) = n | XCL_MEM_TOPOLOGY` 生成 32 个 HBM 伪通道编号，与 ini 的 `HBM[0]` 对齐。

- 一个佐证「模板复用」的细节：DDR 版主机里也残留着同一张 HBM bank 表（[read/DDR/2ports_512bit/src/host.cpp:18-28](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L18-L28)），但在 DDR 版中**从未被使用**——两份 host.cpp 本就同源，只改了 flag 一行。

**diff 实测结论**（本讲义已实际执行，见 4.3.4）：`src/` 目录三份文件中，`krnl_ubench.cpp` 与 `krnl_config.h` 完全一致，`host.cpp` 的差异只有两类——bank flag 两行（`XCL_MEM_DDR_BANK1` ↔ `bank[0]`）与纯粹的缩进/空白。「内核相同、配置不同」不是文档宣称，而是可以亲手验证的事实。

#### 4.3.4 代码实践：亲手 diff 两个工程

**实践目标**：用一条只读命令验证「DDR 与 HBM 的内核源码相同」，并精确枚举全部差异点。

**操作步骤**：

1. 在仓库根目录执行（`git diff --no-index` 用于对比任意两个路径，退出码非零表示有差异，属正常）：

   ```bash
   git diff --no-index \
     ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src \
     ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src
   ```

2. 单独再比一次内核与契约头（两者应零输出）：

   ```bash
   git diff --no-index \
     ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp \
     ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/krnl_ubench.cpp
   git diff --no-index \
     ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h \
     ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/krnl_config.h
   ```

3. 把 ini 也纳入对比（把路径中的 `src` 换成 `ubench.ini` 各自完整路径），记录 `slr`/`sp` 行的差异。

**需要观察的现象 / 预期结果**（已在编写本讲义时实际运行验证）：

- 步骤 1 的输出只涉及 `host.cpp` 一个文件；`krnl_ubench.cpp` 与 `krnl_config.h` 不出现在 diff 中（步骤 2 零输出）。
- `host.cpp` 的语义差异恰好两行：`source_in_ext[i].flags = XCL_MEM_DDR_BANK1;` ↔ `= bank[0];`（两个分支各一处，共两对）。其余全部是 tab/空格缩进差异。
- ini 对比差异：`slr=krnl_ubench_1:SLR1` ↔ `SLR0`；`sp=...DDR[1]` ↔ `sp=...HBM[0]`。

本实践无硬件、无 Vitis 依赖，任何克隆了仓库的机器都能复现。

#### 4.3.5 小练习与答案

**练习 1**：要把 DDR 版工程迁到 HBM 平台，内核代码要改几行？

**答案**：0 行。需要改的是 `ubench.ini` 的 `sp`（`DDR[1]`→`HBM[n]`，并按需调 `slr`）和主机 `flags`（`XCL_MEM_DDR_BANK1`→`bank[n]`），且两处必须指向同一通道。内核源码与内存类型无关。

**练习 2**：DDR 版 `host.cpp` 顶部那张家谱一样的表（L18-L28）是什么？为什么会在 DDR 版里？

**答案**：HBM 32 伪通道的 `BANK_NAME(n) = n | XCL_MEM_TOPOLOGY` 编号表。它是两份同源主机模板复用的遗迹：HBM 版用它填 `flags`，DDR 版从不引用。读代码时应能识别这种「模板遗留」避免误以为 DDR 版也依赖它。

**练习 3**：两个端口的 `sp` 都写 `DDR[1]`，这个工程实际测的是什么场景？如果想测「各占一通道」的聚合带宽该怎么改？

**答案**：测的是双端口**共享同一通道**的争用行为（两台引擎往同一个 DDR 通道发请求）。要测聚合，应把 `in1` 的 `sp` 改到另一通道（如 `DDR[0]`），同时把主机第二个缓冲的 `flags` 改成对应 bank（如 `XCL_MEM_DDR_BANK0`）——注意主机当前对所有端口用同一个 flag，需先把它改成按端口区分。

## 5. 综合实践：把内核扩展为 3 个读端口

把 4.1 的结构对称性变成手感：为内核增加第三个读端口 `in2`，让三台读引擎并发，同步改好主机与 ini，并记录全部改动点。**以下修改建议在一份拷贝目录中进行（例如把 `read/DDR/2ports_512bit/` 复制为 `read/DDR/3ports_512bit/`），不要直接改仓库原有工程**。

**实践目标**：验证你已掌握「端口数 = bundle 数 = 循环份数 = 主机 NUM_PORT = ini sp 行数」这条贯穿内核、主机、连接配置的联动链。

**操作步骤**：

1. **内核 `krnl_ubench.cpp`（5 处改动）**。改后的内核如下（**示例代码**，基于原文件对称扩展）：

   ```cpp
   void krnl_ubench(volatile INTERFACE_WIDTH* in0, volatile INTERFACE_WIDTH* in1,
                    volatile INTERFACE_WIDTH* in2, const int size) {   // ① 新参数，必须放在 size 之前
   #pragma HLS INTERFACE m_axi port=in0 offset=slave bundle=gmem0 max_read_burst_length=16
   #pragma HLS INTERFACE m_axi port=in1 offset=slave bundle=gmem1 max_read_burst_length=16
   #pragma HLS INTERFACE m_axi port=in2 offset=slave bundle=gmem2 max_read_burst_length=16  // ② 新 bundle
   #pragma HLS INTERFACE s_axilite port=in2 bundle=control                                     // ③ 新控制寄存器
       volatile INTERFACE_WIDTH temp_data_2;                                                  // ④ 新 volatile temp
       #pragma HLS DATAFLOW
       // 原有两个循环保持不变，再对称增加第三份：
       for (int i = 0; i < NUM_ITERATIONS; i++) {
           for (int j = 0; j < size; j++) {
   #pragma HLS PIPELINE II=1
               temp_data_2 = in2[j];                                                          // ⑤ 第三台读引擎
           }
       }
       // ...
   ```

   注意**参数顺序陷阱**：`in2` 必须插在 `size` 之前。主机按「第 0..NUM_PORT-1 个参数是缓冲、第 NUM_PORT 个参数是 size」绑定（[host.cpp:157-164](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L157-L164)），若把 `in2` 加到 `size` 之后，setArg 下标全部错位，运行时参数校验会失败。

2. **主机 `host.cpp`（1 处改动）**：只把 [host.cpp:16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L16) 的 `#define NUM_PORT 3`。缓冲向量、setArg 循环、带宽公式都以 `NUM_PORT` 为泛型参数，无需再动。

3. **连接 `ubench.ini`（1 行新增）**：仿照 [ubench.ini:3-4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L3-L4) 增加 `sp=krnl_ubench_1.in2:DDR[1]`（与另两端口共享通道；若改连 `DDR[0]` 则主机对应缓冲的 `flags` 也要按端口区分，见练习 4.3-3）。`nk`、`slr` 不变。

4. **构建验证**（需 Vitis 2020.2，参考 u1-l3）：`make build TARGET=sw_emu DEVICE=<平台>` 后 `make check TARGET=sw_emu` 跑通功能链路。

**需要观察的现象 / 预期结果**：

- 改动清单共 7 处：内核 5 处 + 主机 1 处宏 + ini 1 行 `sp`。
- sw_emu 能编译运行、三个端口均有数据迁移，即功能正确（sw_emu 的带宽数值无物理意义，u1-l3 已结论）。
- 真机上（本机无硬件，**待本地验证**）：三个端口都挤 `DDR[1]` 时，带宽预期**不会**从 38.4 GB/s 线性涨到 57.6 GB/s——共享单通道会饱和；若把 `in2` 分到另一通道，聚合带宽才有台阶式提升。这正是「并发端口数」因素受内存拓扑约束的直接体现。

## 6. 本讲小结

- 内核用「两份对称的循环 + 两个异名 bundle（`gmem0`/`gmem1`）+ 两个互不依赖的 volatile temp」在 `DATAFLOW` 下构造出两台并发读引擎；bundle 名是端口数量的开关，循环份数与 temp 个数是 dataflow 并发的前提。
- 内层 `PIPELINE II=1` 使每端口每拍发一个 512-bit 请求，连续 `j` 扫描被自动合并为 `max_read_burst_length=16` 拍（1024 B）的 AXI 突发；300 MHz 双端口理论峰值 38.4 GB/s。
- 防优化是双保险：volatile **指针**保证每个读请求必须发出，volatile **临时变量**保证读到的值必须被消费；两者齐拆则循环体零副作用、可被整段删除。`NUM_ITERATIONS=10000` 做时间放大，并以魔数 `0.000010000` 的身份藏进主机带宽公式。
- FPGA 读通路无 cache，重复读同一数据不污染测量；要防的是编译期删除，而非 CPU 式的运行期缓存命中。
- DDR 与 HBM 工程的内核源码**逐字节相同**（本讲已用 `git diff --no-index` 实测：仅 `host.cpp` 的两行 bank flag 与空白不同）；内存类型由链接期 `ubench.ini` 的 `sp`/`slr` 和运行期主机 `flags` 分层决定，两处必须手工对齐。
- 两个手写工程的端口都接到同一通道（`DDR[1]`/`HBM[0]`），测的是共享通道争用而非跨通道聚合——解读带宽数字前先看 `sp` 行。

## 7. 下一步学习建议

- **u3-l2（调参数实验）**：把本讲的「加端口」推广成系统性操作——按 datacenter README 的指南改端口数、`DWIDTH` 位宽与 `max_read/write_burst_length`，梳理每个参数的联动文件清单。
- **u3-l3（DDR 与 HBM 连接配置）**：本讲只用了 `sp` 的最简单形态；下一讲深入 `slr`/`sp`/`nk` 三条指令与 U280 的 32 伪通道 HBM 表，解释「端口拆到不同 bank 为何能改变总带宽」。
- **u3-l4（写带宽变体）**：对照 `out0[j] = 常量` 的写内核，看防优化与突发机制在写方向的对称实现。
- 延伸阅读：`ubench/offchip_bandwidth/datacenter/read/` 下其他目录（如不同端口数/位宽组合）与本讲样板的关系，可作为 5 分钟速读练习。
