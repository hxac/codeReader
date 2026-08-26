# DDR 与 HBM：ubench.ini 连接配置与内核布局

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行解释 Vitis 链接配置文件 `ubench.ini` 中 `[connectivity]` 段的三条指令：`slr`（内核实例放到哪个 SLR 分区）、`sp`（内核端口连到哪个内存通道）、`nk`（内核要例化几份）。
2. 解释主机端 `cl_mem_ext_ptr_t` 的 `flags` 字段如何把缓冲区分配到指定内存 bank，以及它为什么必须与 ini 中的 `sp=` 行逐端口对齐。
3. 说出 U200「少量大通道 DDR4」与 U280「32 个小伪通道 HBM2」在配置上的差别，以及 `host.cpp` 顶部那张 32 项 HBM bank 常量表的作用。
4. 独立完成一次「把两个读端口从共享一个 HBM 通道拆到两个通道」的连接改造，并说清楚它为什么可能改变总带宽。

本讲只谈**连接与布局**：内核源码本身一行都不用改。这正好说明 uBench 的一个设计事实——内存类型、通道选择、内核放置这些「布线级」决策全部被推到了链接期配置与主机端 flag 上。

## 2. 前置知识

本讲需要以下前置概念（均在前几讲建立过，这里只做一句话唤醒）：

- **计算单元（CU）**：HLS 内核经 `v++ -l` 链接后在 FPGA 上生成的硬件实例，实例名由 `nk` 指令决定（如 `krnl_ubench_1`）。主机用 `krnl_ubench:{krnl_ubench_1}` 这样的「内核名:CU 名」拿到它的句柄（见 u2-l2）。
- **m_axi 端口**：内核签名里的每个 `bundle` 指针参数对应一个 AXI 主端口（u2-l1）。读带宽内核有两个异名 bundle（`gmem0`/`gmem1`），因此有两个端口 `in0`、`in1`。
- **链接期与运行期**：`v++ -l` 生成 `.xclbin` 时布线已经固化；主机程序运行时只能「顺应」这些连线，不能改变它们（u1-l3、u1-l4）。
- **XRT 与 OpenCL 扩展**：主机通过 XRT 提供的 OpenCL 兼容层访问设备，`cl_mem_ext_ptr_t`、`CL_MEM_EXT_PTR_XILINX`、`XCL_MEM_TOPOLOGY` 等都是 Xilinx 扩展，定义在 Vitis/XRT 安装目录的头文件中（经 `CL/cl_ext_xilinx.h` 引入）——**仓库本身不带这些头文件**，所以它们的位级定义要看本机 Vitis 安装，本讲只讲语义。

如果你还没读过 u3-l1（读带宽内核精读），请先补上：本讲频繁引用它的结论「DDR 版与 HBM 版内核源码完全相同，差异全在连接配置」。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini` | U200 DDR 版连接配置：CU 放 SLR1，双端口都连 `DDR[1]` |
| `ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/ubench.ini` | U280 HBM 版连接配置：CU 放 SLR0，双端口都连 `HBM[0]` |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp` | DDR 版主机程序：`XCL_MEM_DDR_BANK1` 风格 flag；顶部带 32 项 HBM bank 表（模板遗留） |
| `ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/host.cpp` | HBM 版主机程序：`bank[0]`（即 `0 \| XCL_MEM_TOPOLOGY`）风格 flag |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp` | 内核签名，提供 `sp=` 行里引用的端口名 `in0`/`in1` |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile` | 用 `LDCLFLAGS += --config ./ubench.ini` 把 ini 喂给 `v++ -l` |
| `ubench/offchip_bandwidth/datacenter/auto_collect/connectivity_gen.py` | 自动生成 ini 的脚本，佐证 sp 行的生成规则 |
| `ubench/offchip_latency/datacenter/DDR/32bit_per_access/ubench.ini` | 另一个 ini 实例：演示 `DDR[0]` 与多端口（`in0`、`in0_index`）的写法 |

## 4. 核心概念与源码讲解

### 4.1 ini 连接指令：slr / sp / nk

#### 4.1.1 概念说明

`ubench.ini` 是 Vitis 链接器（`v++ -l`）的配置文件，只在一个地方被消费：链接生成 `.xclbin` 的那一步。它回答三个纯「布线」问题：

1. **内核实例放在芯片的哪个物理区域？**（`slr=`）
2. **内核的每个内存端口连到哪条片外存储通道？**（`sp=`）
3. **这个内核要例化几份？**（`nk=`）

三条指令都不改变内核源码的计算行为。也就是说：同一个 `.xo`（内核对象文件），配上不同的 ini，可以变成完全不同的硬件——连 DDR 还是连 HBM、一个实例还是十个实例，全是链接期决策。这正是 uBench 能用「一套内核源码 + 参数化配置」扫遍内存系统设计空间的前提。

三条指令逐条解释：

- `slr=krnl_ubench_1:SLR1`：把名为 `krnl_ubench_1` 的 CU 约束到 SLR1。SLR（Super Logic Region）是 UltraScale+ 器件的物理分区（U200/U280 各有 3 个），跨 SLR 的信号要走长布线，增加延迟与拥塞。惯例是把 CU 放在**靠近它要访问的内存控制器**的 SLR 上。
- `sp=krnl_ubench_1.in0:DDR[1]`：stream port 指令，把 CU `krnl_ubench_1` 的端口 `in0` 连到名为 `DDR[1]` 的内存通道。左边的端口名必须与内核签名里的参数名完全一致；右边的通道名来自 FPGA 平台（shell）提供的内存拓扑描述。
- `nk=krnl_ubench:1`：number of kernels，把内核 `krnl_ubench` 例化 1 份，生成的实例命名为 `krnl_ubench_1`（内核名 + 下划线 + 从 1 起的序号）。若写 `nk=krnl_ubench:2`，会得到 `krnl_ubench_1` 与 `krnl_ubench_2` 两个实例。

#### 4.1.2 核心流程

从源码到运行的完整链路中，ini 只在链接一步生效：

```text
src/krnl_ubench.cpp
      │  v++ -c（编译，读内核源码与 HLS pragma）
      ▼
_x.hw.<XSA>/krnl_ubench.xo          ← 内核对象文件（与内存类型无关）
      │  v++ -l --config ./ubench.ini（链接，读 ini）
      ▼
build_dir.hw.<XSA>/ubench.xclbin    ← CU 放置、端口-通道连线已固化
      │  主机加载 xclbin，按 CU 名创建 Kernel
      ▼
host.cpp 运行：flag 决定的缓冲区所在 bank 必须与连线两端匹配
```

三条指令生效时刻的对照表：

| 指令 | 生效时刻 | 决定什么 | 谁必须与之对齐 |
| --- | --- | --- | --- |
| `nk` | 链接期 | 实例个数与实例名 | 主机端 CU 名拼接代码 |
| `slr` | 链接期 | CU 的物理放置 | 无（对主机透明） |
| `sp` | 链接期 | 端口 → 内存通道 | 主机端缓冲区 flags |

如果用聚合带宽的视角看 `sp`，两端口共享一条通道与各连一条通道的理论上限完全不同。设单通道带宽上限为 \( B_{ch} \)，端口数为 \( P \)，每端口理论需求为 \( B_{port} \)，则：

\[ B_{agg}^{shared} = \min\left(P \cdot B_{port},\; B_{ch}\right) \le B_{ch} \]

\[ B_{agg}^{split} = \min\left(P \cdot B_{port},\; k \cdot B_{ch}\right) \]

其中 \( k \) 是端口实际用到的互不相同的通道数。当前仓库示例里 DDR 版与 HBM 版都是 \( k = 1 \)（两端口分别都连 `DDR[1]` / 都连 `HBM[0]`），所以测的是**共享通道下的争用行为**——这是 u3-l1 已经给出的结论，本讲从配置文件层面把它坐实。

#### 4.1.3 源码精读

先看 DDR 版 ini 全文（6 行就是全部）：

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini:L1-L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L1-L6)

```ini
[connectivity]
slr=krnl_ubench_1:SLR1
sp=krnl_ubench_1.in0:DDR[1]
sp=krnl_ubench_1.in1:DDR[1]

nk=krnl_ubench:1
```

- 第 1 行段名 `[connectivity]` 声明后面是连接约束段。
- 第 2 行把实例 `krnl_ubench_1` 放到 SLR1。
- 第 3–4 行把两个端口 `in0`、`in1` **都**连到 `DDR[1]`——共享同一通道。
- 第 6 行声明只例化 1 份内核，于是左侧 CU 名是 `krnl_ubench_1` 而不是 `krnl_ubench_2`。

再看 HBM 版 ini 全文：

[ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/ubench.ini:L1-L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/ubench.ini#L1-L6)

```ini
[connectivity]
slr=krnl_ubench_1:SLR0
sp=krnl_ubench_1.in0:HBM[0]
sp=krnl_ubench_1.in1:HBM[0]

nk=krnl_ubench:1
```

与 DDR 版只有两处不同：`SLR0`（U280 上 HBM 堆栈的访问通路在 SLR0 一侧，CU 靠近它放置）与 `HBM[0]`（32 个 HBM 伪通道中的第 0 个）。结构完全同构——印证了 u3-l1 的结论：**内存类型差异完全落在 ini 与主机 flag，内核一字不改**。

`sp=` 行左边的端口名从哪来？来自内核签名的参数名。[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp:L4-L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L4-L6) 中签名是 `krnl_ubench(volatile INTERFACE_WIDTH* in0, volatile INTERFACE_WIDTH* in1, const int size)`，两个指针参数名 `in0`/`in1` 就是 ini 里 `krnl_ubench_1.in0`、`krnl_ubench_1.in1` 冒号前的端口名。改内核参数名而不改 ini，链接会直接报端口找不到的错误。

`nk` 如何影响主机？看 CU 名的拼接代码：

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp:L73-L76](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L73-L76)

```cpp
std::string cu_id = std::to_string(i + 1);
std::string krnl_name_full =
    krnl_name + ":{" + krnl_name + "_" + cu_id + "}";
```

主机按 `i+1` 拼 `krnl_ubench_1`，与 `nk=krnl_ubench:1` 的实例命名规则（内核名_序号，从 1 起）严格耦合。若把 ini 改成 `nk=krnl_ubench:2` 而主机 `NUM_KERNEL` 仍是 1，多出的 `krnl_ubench_2` 只会被闲置；反之主机 `NUM_KERNEL=2` 而 ini 仍是 1，创建第二个 Kernel 对象时会失败。

ini 在哪里被消费？在 Makefile 的链接变量里：

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile:L72-L73](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L72-L73)

```make
# Kernel linker flags
LDCLFLAGS += --config ./ubench.ini
```

而 [Makefile:L98-L100](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L98-L100) 用 `$(VPP) $(CLFLAGS) --temp_dir $(BUILD_DIR) -l $(LDCLFLAGS) ...` 执行真正的链接。也就是说：**改完 ini 必须重跑 `v++ -l`（重新 make）才生效，运行期无法切换**。

最后看自动生成器如何产出这些行——这是 sp 行规则的最直接佐证：

[ubench/offchip_bandwidth/datacenter/auto_collect/connectivity_gen.py:L10-L21](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/connectivity_gen.py#L10-L21)

```python
connectivity_file.append('slr=krnl_ubench_1:SLR0' + '\n')

for port_index in range(num_concurrent_port):
    if (access_type == 'RD'):
        connectivity_file.append('sp=krnl_ubench_1.in' + str(port_index) + ':' + bank_name + '\n')
    elif (access_type == 'WR'):
        connectivity_file.append('sp=krnl_ubench_1.out' + str(port_index) + ':' + bank_name + '\n')
    ...
connectivity_file.append('nk=krnl_ubench:1' + '\n')
```

生成器循环拼出 `in0`、`in1`…，每行都接**同一个** `bank_name`——这就是为什么仓库里所有手写示例的双端口都共享一条通道：模板本身只支持「多端口 → 单 bank」这一种拓扑，想拆通道必须手工改（本讲综合实践就做这件事）。

顺带看一个多端口 ini 的另一个真实例子（延迟微基准）：

[ubench/offchip_latency/datacenter/DDR/32bit_per_access/ubench.ini:L1-L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_latency/datacenter/DDR/32bit_per_access/ubench.ini#L1-L6)

```ini
[connectivity]
slr=krnl_ubench_1:SLR0
sp=krnl_ubench_1.in0:DDR[0]
sp=krnl_ubench_1.in0_index:DDR[0]

nk=krnl_ubench:1
```

它的内核有两个端口：数据数组 `in0` 与下标数组 `in0_index`，两行 `sp=` 分别把两个端口连到 `DDR[0]`。注意这里用的是 `DDR[0]` 而读带宽工程用 `DDR[1]`——**bank 序号是自由选择**，同一块 U200 上两条 DDR 通道都可用。

#### 4.1.4 代码实践

**实践目标**：确认「ini 三条指令 ↔ 内核签名 ↔ 主机 CU 命名」三方的对应关系，并验证 DDR/HBM 两版 ini 的差异恰好只有两行。

**操作步骤**：

1. 在仓库根目录执行（只读命令，不改任何文件）：

   ```bash
   diff ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini \
        ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/ubench.ini
   ```

2. 对着 diff 输出数一数：有几行不同？分别是哪条指令的哪个字段？
3. 打开内核源码 `read/DDR/2ports_512bit/src/krnl_ubench.cpp`，在签名里找到 ini 冒号前出现的两个端口名。
4. 心算推演：把 ini 改成 `nk=krnl_ubench:2` 后，`v++ -l` 会生成哪几个 CU 名？主机 [host.cpp:L73-L76](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L73-L76) 的 `cu_id` 循环要配多大的 `NUM_KERNEL` 才能全部用上？

**需要观察的现象**：diff 只报出 `slr` 行与两处 `sp` 行的通道名不同（`SLR1`→`SLR0`、`DDR[1]`→`HBM[0]`），`nk` 行与段名完全一致。

**预期结果**：差异共 3 行（1 行 slr + 2 行 sp 的通道名部分）。第 4 步的答案：CU 名为 `krnl_ubench_1`、`krnl_ubench_2`，`NUM_KERNEL` 需为 2。（本实践的 diff 已可静态完成，无需 Vitis 环境。）

#### 4.1.5 小练习与答案

**练习 1**：`sp=krnl_ubench_1.in0:DDR[1]` 中，如果把内核签名的 `in0` 重命名为 `data0` 而忘记改 ini，会发生什么？

**答案**：链接期 `v++ -l` 报错退出——ini 引用的端口 `krnl_ubench_1.in0` 在内核里已不存在。`sp` 的端口名与内核参数名是硬绑定关系，这也是改端口名时「内核 + ini」两处联动的根源（承接 u3-l2 的四处联动清单，ini 是其中第四处）。

**练习 2**：`nk=krnl_ubench:1` 与主机 `#define NUM_KERNEL 1`（[host.cpp:L15](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L15)）各管什么？为什么说它们是「同一数量的两端」？

**答案**：`nk` 在链接期决定硬件里实际放下几个 CU；`NUM_KERNEL` 在编译期决定主机创建并启动几个 Kernel 对象、循环设置几组参数。数量必须一致：硬件多放则闲置浪费资源，主机多建则创建 Kernel 对象失败。

**练习 3**：为什么 `slr` 指令对主机程序完全透明，而 `sp` 不是？

**答案**：`slr` 只约束 CU 在芯片物理分区上的位置，不改变任何逻辑接口或内存可见性，主机无从感知也无需感知；`sp` 决定端口物理上连到哪条存储通道，而主机分配缓冲区时必须把数据放进**端口可达的那条通道**，所以主机必须知道（并配合）`sp` 的选择——这就是 4.2 节的主题。

### 4.2 主机 bank flag：cl_mem_ext_ptr_t 与 ini 的对齐

#### 4.2.1 概念说明

`sp=` 只解决了「端口的电线插到哪条通道」。还有一个对称的问题：**主机搬运进设备的缓冲区放在哪条通道**。OpenCL 标准里 `cl::Buffer` 并不让你指定物理内存位置，Xilinx 的扩展结构体 `cl_mem_ext_ptr_t` 补上了这个洞：

```cpp
typedef struct _cl_mem_ext_ptr_t {
    void*  obj;    // 指向主机侧内存
    size_t param;  // 保留字段，填 0
    unsigned flags; // 关键字段：指定设备侧落在哪个内存（bank/拓扑编号）
} cl_mem_ext_ptr_t;
```

创建 Buffer 时把 `CL_MEM_EXT_PTR_XILINX` 标志与该结构体指针一起传入，XRT 就会把设备侧存储分配到 `flags` 指定的通道。

`flags` 有两种风格，正好被仓库的 DDR 版与 HBM 版各用一种：

1. **bank 编号风格（DDR 版）**：直接填 `XCL_MEM_DDR_BANK1` 这类宏，表示「分配到 DDR bank 1」，与 ini 里的 `sp=...:DDR[1]` 对应。
2. **拓扑编号风格（HBM 版）**：填 `n | XCL_MEM_TOPOLOGY`。`XCL_MEM_TOPOLOGY` 是一个位标志，告诉 XRT「flags 的其余位按平台内存拓扑编号解释」；对 U280，编号 n 就是第 n 个 HBM 伪通道，与 ini 里的 `sp=...:HBM[n]` 对应。

两种风格的宏都定义在 Vitis/XRT 安装的头文件中（经 `CL/cl_ext_xilinx.h` 引入，`host.cpp` 第 10 行 include），仓库内不含其定义，位级细节请查本机 Vitis 安装目录。

「对齐」的确切含义有两个维度，缺一不可：

| 维度 | ini 一侧 | 主机一侧 |
| --- | --- | --- |
| CU 名 | `sp=krnl_ubench_1.in0:...` 的 `krnl_ubench_1` | 创建 Kernel 时拼出的 `krnl_ubench:{krnl_ubench_1}` |
| 内存通道 | `:DDR[1]` / `:HBM[n]` | flag `XCL_MEM_DDR_BANK1` / `bank[n]` |

若通道不对齐：端口连到通道 A、缓冲区落在通道 B，CU 从端口读到的不是主机灌入的数据。具体表现依 XRT 版本与平台而异——可能启动时报错，也可能读到无效数据（**待本地验证**：这是文档未明说的失败模式，读者在真机上试错一次最能建立直觉）。

#### 4.2.2 核心流程

主机为每个端口分配缓冲区的流程（以读带宽工程为例，`NUM_KERNEL=1`、`NUM_PORT=2`，共 2 个缓冲区）：

```text
对每个缓冲区 i ∈ [0, NUM_KERNEL*NUM_PORT):
  1. source_in_ext[i].obj    = read_source.data()   ← 主机源数据
  2. source_in_ext[i].param  = 0                    ← 保留
  3. source_in_ext[i].flags  = <通道选择>            ← DDR 版: XCL_MEM_DDR_BANK1
                                                        HBM 版: bank[0]
  4. cl::Buffer(..., CL_MEM_READ_ONLY | CL_MEM_EXT_PTR_XILINX
                   | CL_MEM_USE_HOST_PTR, ..., &source_in_ext[i])
  5. q.enqueueMigrateMemObjects({buffer}, 0)        ← 数据搬入设备指定通道
```

注意第 3 步的通道选择必须与该缓冲区将来通过 `setArg` 绑到哪个端口一致：`source_in_buffer[i*NUM_PORT+j]` 之后会绑到内核 `i` 的端口 `j`，而端口 `j` 的连线由 ini 的第 `j` 条 `sp=` 行决定。当前仓库两版都把所有缓冲区放进同一个通道，所以循环里写死一个 flag 就够；一旦拆通道（综合实践），flag 就得随端口序号变化。

#### 4.2.3 源码精读

先看 DDR 版的 flag 赋值：

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp:L113-L128](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L113-L128)

```cpp
// For Allocating Buffer to specific Global Memory Bank, user has to use cl_mem_ext_ptr_t
// and provide the Banks
if (xcl::is_emulation()) {
    for (int i = 0; i < NUM_KERNEL*NUM_PORT; i++) {
        source_in_ext[i].obj = read_source.data();
        source_in_ext[i].param = 0;
        source_in_ext[i].flags = XCL_MEM_DDR_BANK1;
    }
}
else {
    for (int i = 0; i < NUM_KERNEL*NUM_PORT; i++) {
        source_in_ext[i].obj = read_source.data();
        source_in_ext[i].param = 0;
        source_in_ext[i].flags = XCL_MEM_DDR_BANK1;
    }
}
```

`XCL_MEM_DDR_BANK1` 与 ini 的 `DDR[1]` 精确对应。同时注意一个 u2-l2 已揭过的模板痕迹：`is_emulation()` 分支与 `else` 分支**逐字相同**，这里没有为仿真做任何 bank 特判——改 flag 时两个分支都得改（或者干脆合并成一个循环）。

再看 Buffer 创建与搬运：

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp:L133-L147](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L133-L147)

```cpp
OCL_CHECK(err, source_in_buffer[i] =
                cl::Buffer(context,
                            CL_MEM_READ_ONLY | CL_MEM_EXT_PTR_XILINX |
                                CL_MEM_USE_HOST_PTR,
                            sizeof(int) * dataSize,
                            &source_in_ext[i],
                            &err));
// Copy input data to Device Global Memory
OCL_CHECK(err,
            err = q.enqueueMigrateMemObjects(
                {source_in_buffer[i]},
                0 /* 0 means from host*/));
```

三个标志各司其职：`CL_MEM_EXT_PTR_XILINX` 启用扩展指针（flags 字段生效）；`CL_MEM_USE_HOST_PTR` 复用已对齐的主机内存做零拷贝（`aligned_allocator` 保证 4096 字节对齐，见 u2-l2）；`CL_MEM_READ_ONLY` 对应读端口。`enqueueMigrateMemObjects` 把数据从主机搬进 flag 指定的设备通道——这一步在计时窗口之外（u2-l3）。

然后是 HBM 版的 flag 赋值：

[ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/host.cpp:L113-L128](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/host.cpp#L113-L128)

```cpp
if (xcl::is_emulation()) {
    for (int i = 0; i < NUM_KERNEL*NUM_PORT; i++) {
        source_in_ext[i].obj = read_source.data();
        source_in_ext[i].param = 0;
        source_in_ext[i].flags = bank[0];
    }
}
else{
    for (int i = 0; i < NUM_KERNEL*NUM_PORT; i++) {
        source_in_ext[i].obj = read_source.data();
        source_in_ext[i].param = 0;
        source_in_ext[i].flags = bank[0];
    }
}
```

与 DDR 版唯一的实质差异是 `XCL_MEM_DDR_BANK1` 换成了 `bank[0]`——这就是 4.2.1 说的拓扑风格。`bank[0]` 的定义在文件顶部：

[ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/host.cpp:L18-L28](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/host.cpp#L18-L28)

```cpp
//HBM Banks requirements
#define MAX_HBM_BANKCOUNT 32
#define BANK_NAME(n) n | XCL_MEM_TOPOLOGY
const int bank[MAX_HBM_BANKCOUNT] = {
    BANK_NAME(0),  BANK_NAME(1),  BANK_NAME(2),  ...  BANK_NAME(31)};
```

（上面省略号处原文件是逐项列出的 32 个元素。）`BANK_NAME(n)` 宏把编号 n 与 `XCL_MEM_TOPOLOGY` 按位或，得到「第 n 个 HBM 伪通道」的 flag；`bank[n]` 因此成为第 n 通道的现成索引。它与 ini 的 `HBM[n]` 靠同一个 n 对齐。这张表的用途详解见 4.3.3。

最后看缓冲区与端口的绑定关系，即「第 i 个缓冲区最终喂给哪个端口」：

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp:L157-L164](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L157-L164)

```cpp
for (i = 0; i < NUM_KERNEL; i++) {
    for (j = 0; j < NUM_PORT; j++) {
        OCL_CHECK(err, err = cmpt_krnl[i].setArg(j, source_in_buffer[i*NUM_PORT+j]));
    }
    OCL_CHECK(err, err = cmpt_krnl[i].setArg(j, dataSize));
    //Invoking the compute kernels
    OCL_CHECK(err, err = q.enqueueTask(cmpt_krnl[i]));
}
```

`setArg(j, ...)` 把 `source_in_buffer[i*NUM_PORT+j]` 绑到内核 `i` 的第 `j` 个参数（即端口 `j`）。所以完整的对齐链条是：**flag（缓冲所在通道）→ buffer → setArg 编号 j → 内核端口 inj → ini 第 j 条 sp 行（端口所连通道）**。链条首尾两个通道必须是同一条。

#### 4.2.4 代码实践

**实践目标**：完成一次「换通道」的最小改动，体会 ini 与 flag 的两端联动。

**操作步骤**：

1. 把 `read/DDR/2ports_512bit` 目录复制一份作为你的实验目录（不污染原工程），例如 `read/DDR/2ports_512bit_bank0`。
2. 在实验目录的 `ubench.ini` 里把三处 `1` 改为 `0`：`DDR[1]`→`DDR[0]`（两行 sp）。`slr` 保持 `SLR1` 不动。
3. 在实验目录的 `src/host.cpp` 里把两个分支的 `XCL_MEM_DDR_BANK1` 都改为 `XCL_MEM_DDR_BANK0`（两个分支共 2 处）。
4. 若本机装有 Vitis，可跑 `make build TARGET=sw_emu DEVICE=<U200 平台>` 验证链接与编译通过；没有 Vitis 则手工核对：列出你改动过的所有行号，检查是否还有遗漏的 `DDR[1]`/`BANK1`（用 `grep -rn "DDR\[1\]\|DDR_BANK1" .`）。

**需要观察的现象**：`grep` 在实验目录中不再命中任何 `DDR[1]` 或 `XCL_MEM_DDR_BANK1`；对照旁证工程 `ubench/offchip_latency/datacenter/DDR/32bit_per_access/ubench.ini`（它本来就用 `DDR[0]`），你的 ini 与它风格一致了。

**预期结果**：共改 4 行（ini 2 行 + host.cpp 2 处 flag）。在真机（U200）上这个工程的行为应与原版几乎相同——因为 U200 两条 DDR 通道规格一致，只是数据落在了另一条通道。sw_emu 下连接约束大多不被严格检查，能否报出通道不匹配的错误依版本而异（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 host.cpp 的 flag 改成 `XCL_MEM_DDR_BANK0` 而 ini 仍是 `DDR[1]`（只改一端），程序会在编译期、链接期还是运行期暴露问题？

**答案**：编译期与链接期都不会有任何报错——g++ 只看宏是否定义，`v++ -l` 只看 ini 自身合法性。问题暴露在**运行期**：缓冲区落在 DDR[0]，而端口连着 DDR[1]，CU 读到的不是主机灌入的数据（具体是报错还是读到无效数据依 XRT 版本而定，见 4.2.1）。这正是「两端口对齐」最容易踩的坑：它是跨工具的口头契约，没有编译器帮你检查。

**练习 2**：为什么 `source_in_ext` 与 `source_in_buffer` 的大小是 `NUM_KERNEL*NUM_PORT` 而不是 `NUM_PORT`？

**答案**：每个内核实例的每个端口都要有自己的缓冲区。`NUM_KERNEL=1` 时两者恰好相等；一旦 `nk` 与 `NUM_KERNEL` 提到 2，就需要 2×2=4 个缓冲区，且第 `i` 个内核用第 `i*NUM_PORT+j` 个——`setArg` 循环（[host.cpp:L157-L164](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L157-L164)）正是按这个下标公式取用的。

**练习 3**：读带宽内核两个端口读的是**同一份**主机数据（两个 ext 的 `obj` 都指向 `read_source.data()`）。既然数据相同，把两端口连到两条不同通道后，带宽测试还有意义吗？

**答案**：有意义。带宽微基准测的是**通路**而非数据语义——内核并不消费读到的值（volatile 临时变量只为防止读被优化掉，见 u3-l1）。两个端口各自从自己通道的副本里顺序搬运，测的是两条通路并行时的聚合吞吐；数据内容是否相同无关紧要。这也是为什么拆通道后公式 `bw = payload*4*0.000010000/t*NUM_KERNEL*NUM_PORT` 里乘 `NUM_PORT` 依然合法。

### 4.3 DDR 与 HBM 差异：U200 双通道 vs U280 32 伪通道

#### 4.3.1 概念说明

两种内存在「通道数 × 单通道带宽」的拓扑上处于两个极端：

| 维度 | U200 DDR4 | U280 HBM2 |
| --- | --- | --- |
| 通道数 | 2 条（`DDR[0]`、`DDR[1]`） | 32 个伪通道（`HBM[0]`…`HBM[31]`） |
| 单通道位宽 | 72bit ECC 的 DDR4 控制器通道，容量大 | 每伪通道 32bit @ 高时钟，容量小（共 8GB） |
| 配置名 | `DDR[n]`，n∈{0,1} | `HBM[n]`，n∈[0,32) |
| 主机 flag | `XCL_MEM_DDR_BANKn` | `n \| XCL_MEM_TOPOLOGY`（即 `bank[n]`） |
| 推荐放置 | CU 靠近 DDR 控制器所在 SLR | CU 靠近 HBM 堆栈访问通路（SLR0 一侧） |

（表中「位宽/容量」是平台常识性描述，具体规格以 Xilinx 官方文档为准；仓库代码只体现通道名与数量。）

设计含义截然不同：

- **DDR（少而宽）**：通道少，两个端口想不争用都没有第二条好选；并发扩展主要靠**加宽端口**（512bit）或提高突发效率。这就是 datacenter README 示例双端口共享 `DDR[1]` 的现实背景。
- **HBM（多而窄）**：32 个独立伪通道天然适合「每个端口独占一条通道」的并行扩展；但单通道窄，靠**多端口各自挂一条**堆出聚合带宽。要吃满 HBM，通常需要 10+ 个端口（KNN 案例正是这么做的，见 u6）。

用 4.1.2 的公式表述：DDR 上 \( k \) 的上限是 2，HBM 上 \( k \) 的上限是 32——但每个 \( B_{ch} \) 小得多。仓库的 HBM 示例只用了 `HBM[0]` 一条（\( k=1 \)），是「共享争用」测法的 HBM 版，而不是 HBM 的聚合峰值测法。

还有一个值得指出的仓库细节：**DDR 版 host.cpp 顶部也原样带着那张 32 项 HBM bank 表**（[DDR/src/host.cpp:L18-L28](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L18-L28) 与 HBM 版逐字相同），但 DDR 版从不使用它——它写死 `XCL_MEM_DDR_BANK1`。这是从 HBM 模板复制工程时的遗留物，读代码时不要被「DDR 工程为什么有 HBM 表」迷惑；反过来它也提醒你：删掉这张表对 DDR 版毫无影响。

#### 4.3.2 核心流程

当要把 P 个端口分配到 HBM 通道时，通用的分配逻辑：

```text
输入: P 个端口 (in0..in_{P-1}), HBM 通道池 [0..31]
分配策略:
  共享: 所有端口 → HBM[0]                    (仓库现状, k=1)
  独占: 端口 j → HBM[j mod 32]               (k=min(P,32))
产出:
  ini:  P 行 sp=krnl_ubench_1.inj:HBM[assign(j)]
  host: source_in_ext[i].flags = bank[assign(i % NUM_PORT)]
```

对应到带宽上限的变化（设每通道上限 \( B_{pc} \)、端口需求 \( B_{port} \)）：

\[ \text{共享: } B_{agg} \le B_{pc}, \qquad \text{独占: } B_{agg} \le \min\left(P \cdot B_{port},\; \min(P,32) \cdot B_{pc}\right) \]

两个注意点：

1. **数据搬运也受益**：`enqueueMigrateMemObjects` 写入两个不同通道时可以并行完成，灌数据阶段更快（虽然它在计时窗口外）。
2. **拆分不是免费午餐**：32 个伪通道共享同一颗 HBM 堆栈的内部总线与刷新机制，实测聚合带宽并非严格线性于 \( k \)；且更多通道意味着更复杂的布线，可能压低可实现频率。这正是「微基准实测」存在的意义——理论公式只给上限。

#### 4.3.3 源码精读

先看 32 项 bank 表在 HBM 版中的定义（它就是「独占分配」的原料）：

[ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/host.cpp:L18-L28](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/host.cpp#L18-L28)

```cpp
//HBM Banks requirements
#define MAX_HBM_BANKCOUNT 32
#define BANK_NAME(n) n | XCL_MEM_TOPOLOGY
const int bank[MAX_HBM_BANKCOUNT] = {
    BANK_NAME(0),  BANK_NAME(1),  ..., BANK_NAME(31)};
```

这张表的用途分三点：

1. **把拓扑编号变成合法 flag**：`XCL_MEM_TOPOLOGY` 风格的 flag 不能只写裸数字 n，必须 `n | XCL_MEM_TOPOLOGY`；`BANK_NAME` 宏封装了这个按位或。
2. **提供可索引的常量池**：主机代码用 `bank[j]` 就能为端口 j 取到第 j 通道的 flag，不必在循环里手写宏拼接（C 里也没法拼宏名）。
3. **`MAX_HBM_BANKCOUNT=32` 显式记录平台事实**：U280 恰有 32 个 HBM 伪通道，端口数超过 32 时必须复用通道（`j mod 32`）。

对照 DDR 版同位置的表（一字不差却无人使用）：

[ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp:L18-L28](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L18-L28)

```cpp
//HBM Banks requirements
#define MAX_HBM_BANKCOUNT 32
#define BANK_NAME(n) n | XCL_MEM_TOPOLOGY
const int bank[MAX_HBM_BANKCOUNT] = { ... };
```

DDR 版的 flag 赋值（4.2.3 已精读）写的是 `XCL_MEM_DDR_BANK1`，从不引用 `bank[]`——模板复制的遗留物。

最后把两侧配置放进一张对照表，这是本讲最需要记住的东西：

| 配置点 | DDR 版（U200） | HBM 版（U280） |
| --- | --- | --- |
| ini `slr` | `krnl_ubench_1:SLR1` | `krnl_ubench_1:SLR0` |
| ini `sp` | `in0`、`in1` → `DDR[1]`（共享） | `in0`、`in1` → `HBM[0]`（共享） |
| ini `nk` | `krnl_ubench:1` | `krnl_ubench:1` |
| host flag | `XCL_MEM_DDR_BANK1`（两分支同） | `bank[0]`（两分支同） |
| bank 表 | 存在但未使用 | 被 `bank[0]` 使用 |
| 内核源码 | 相同（u3-l1 已 diff 验证） | 相同 |

#### 4.3.4 代码实践

**实践目标**：解释 32 bank 常量表，并写出「按端口取通道」的 flag 改法（完整改造留到综合实践）。

**操作步骤**：

1. 读 [HBM/src/host.cpp:L20](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/host.cpp#L20) 的 `BANK_NAME` 宏，写出 `bank[3]` 展开后的表达式。
2. 把 4.2.3 中 flag 循环里的 `bank[0]` 心算替换为 `bank[i % NUM_PORT]`（循环变量 `i` 遍历 `NUM_KERNEL*NUM_PORT` 个缓冲区），推演 `NUM_KERNEL=1, NUM_PORT=2` 时两个缓冲区各落到哪个通道。
3. 写出与第 2 步配套的 ini 修改（两行 `sp=` 各改成什么）。
4. 回答：若 `NUM_PORT=40`，`bank[i % NUM_PORT]` 会怎样？需要什么保护？

**需要观察的现象**：纯纸面推演，无运行输出。

**预期结果**：第 1 步 `bank[3] = 3 | XCL_MEM_TOPOLOGY`；第 2 步缓冲区 0 → `HBM[0]`、缓冲区 1 → `HBM[1]`；第 3 步 `sp=krnl_ubench_1.in0:HBM[0]`、`sp=krnl_ubench_1.in1:HBM[1]`；第 4 步 `i % NUM_PORT` 会取到 `bank[8]`…`bank[39]`，数组越界（表长 32）——正确写法是 `bank[i % MAX_HBM_BANKCOUNT]`，让第 33 个端口起复用通道 0（**待本地验证**：越界读取在 C++ 中是未定义行为，不会编译报错，必须自己防）。

#### 4.3.5 小练习与答案

**练习 1**：U200 上只有两条 DDR 通道，为什么示例仍让双端口共享 `DDR[1]` 而不是拆到 `DDR[0]`/`DDR[1]`？

**答案**：两方面原因。其一，微基准的一个测法维度就是「共享通道下的争用行为」——两端口抢一条通道，观察带宽如何低于单端口理想值的两倍，这本身就是数据点；其二，U200 单通道容量大、位宽宽，共享一条通道已足以跑满双 512bit 端口的多数场景（300MHz×2×512bit≈38.4GB/s 的理论峰值与单 DDR4 通道带宽同量级，见 u2-l1）。而 connectivity_gen.py 的模板只生成「同 bank」的 sp 行，也固化了这种测法。

**练习 2**：HBM 版把 CU 放在 `SLR0`，DDR 版放在 `SLR1`。如果故意把 HBM 版的 CU 放到 `SLR2`，程序还能跑吗？会有什么变化？

**答案**：能跑。`slr` 只是物理放置约束，不影响逻辑正确性——布线工具会自动把端口连到 HBM 通道（`sp` 行说了算），只是所有访问都要跨 SLR 长布线，时序变差、可实现频率可能下降，最终拉低实测带宽。微基准对频率敏感（带宽正比于频率），所以 `slr` 虽「对主机透明」，却实实在在影响测量结果。

**练习 3**：DDR 版 `host.cpp` 顶部那张未使用的 HBM bank 表，有没有办法让它变得「有用」？

**答案**：有，但方向是换平台而非换代码——把工程移植到 U280 时，把 flag 从 `XCL_MEM_DDR_BANK1` 改为 `bank[n]` 即可直接复用这张表。这也解释了它为何留在 DDR 版里：整份 host.cpp 是从 HBM 模板复制来的（与 u3-l1 内核同源的复制链），删表无收益、留着无害，移植时反而省事。

## 5. 综合实践

**任务：把 read/HBM 工程的两个读端口从共享 `HBM[0]` 拆分为 `HBM[0]` + `HBM[1]`，并论证它为什么可能改变总带宽。**

这是本讲规格指定的完整改造，把 4.1 的 ini、4.2 的 flag、4.3 的 bank 表全部串起来。

**操作步骤**：

1. 复制工程目录：

   ```bash
   cp -r ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit \
         ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit_split
   ```

2. 改 `ubench.ini` 的两行 `sp=`（[HBM/ubench.ini:L3-L4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/ubench.ini#L3-L4)）：

   ```ini
   sp=krnl_ubench_1.in0:HBM[0]
   sp=krnl_ubench_1.in1:HBM[1]
   ```

   `slr` 与 `nk` 行不动。

3. 改 `src/host.cpp` flag 赋值（[L115-L128](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/HBM/2ports_512bit/src/host.cpp#L115-L128)），两个分支都把 `bank[0]` 换成按端口序号取通道：

   ```cpp
   // 示例代码：按端口独占一条 HBM 伪通道
   for (int i = 0; i < NUM_KERNEL*NUM_PORT; i++) {
       source_in_ext[i].obj = read_source.data();
       source_in_ext[i].param = 0;
       source_in_ext[i].flags = bank[i % NUM_PORT];  // 缓冲区 i 绑端口 i%NUM_PORT
   }
   ```

   注意缓冲区下标 `i*NUM_PORT+j` 将来绑到端口 `j`（4.2.3 的 setArg 链条），所以这里用 `i % NUM_PORT` 取通道才能保证「缓冲区所在通道 == 端口所连通道」。

4. 自查对齐链条：flag `bank[j]` ↔ ini 第 j 行 `HBM[j]` ↔ setArg 编号 j ↔ 内核参数 inj，四个环节通道号一致。

5. 有 Vitis + U280 真机时：`make check TARGET=hw DEVICE=<U280 平台>`，对比改造前后各 payload 档（1KB→1MB）的带宽输出；只有 sw_emu 时可以验证流程跑通，但**带宽数值无物理意义**（u1-l3 的结论），拆分与否在 sw_emu 里看不出差别。

**为什么拆分可能改变总带宽（论证要点）**：

- 拆分前，两个端口的全部读请求最终汇聚到同一个 HBM 伪通道，\( B_{agg} \le B_{pc} \)，双端口在通道级互相争用；
- 拆分后，各走一条伪通道，\( B_{agg} \le \min(2 B_{port},\, 2 B_{pc}) \)，理论上限翻倍；
- 但 HBM 伪通道共享堆栈内部总线与刷新，实测增益通常低于 2 倍；且不同 `slr` 放置下时序变化也可能掺入影响——这正是需要微基准实测、而非只信公式的原因。

**32 项 bank 常量表在其中的角色**（对照解释，见 4.3.3）：`MAX_HBM_BANKCOUNT=32` 对应 U280 的 32 个 HBM 伪通道；`BANK_NAME(n)=n|XCL_MEM_TOPOLOGY` 把「拓扑编号 n」编码成合法 flag；`bank[n]` 使主机可以像查表一样为第 n 个端口取第 n 条通道。没有这张表，第 3 步的 `bank[i % NUM_PORT]` 就得写成裸的 `(i % NUM_PORT) | XCL_MEM_TOPOLOGY`，既难读又易漏掉拓扑标志。

**预期结果（真机）**：大 payload 档（≥256KB）带宽应明显高于共享版；小 payload 档差异被启动开销淹没（u2-l3）。具体倍率**待本地验证**。

## 6. 本讲小结

- `ubench.ini` 是 `v++ -l` 的链接期布线表（经 Makefile 的 `--config` 喂入），三条指令各管一件事：`slr` 定 CU 物理放置、`sp` 定端口到内存通道的连线、`nk` 定实例个数与命名。
- `nk` 的实例命名（`krnl_ubench_1`）与主机 CU 名拼接代码严格耦合；`sp` 的端口名与内核参数名严格耦合——改任何一端都必须两端联动。
- 主机端 `cl_mem_ext_ptr_t` 的 `flags` 决定缓冲区落在哪条通道，必须与 `sp=` 行逐端口对齐；这是跨 g++/v++ 两个工具的口头契约，没有编译期检查，错了在运行期才暴露。
- 两种 flag 风格：DDR 用 `XCL_MEM_DDR_BANK1`（bank 编号式），HBM 用 `bank[n] = n | XCL_MEM_TOPOLOGY`（拓扑编号式）；宏定义在 Vitis/XRT 安装头文件中，仓库不含。
- U200 是「2 条宽 DDR 通道」、U280 是「32 条窄 HBM 伪通道」，`MAX_HBM_BANKCOUNT=32` 与 `bank[]` 表就是后者的主机侧落点；两版示例内核源码完全相同，全部差异在 ini 的 `slr`/`sp` 与主机 flag。
- 仓库示例的双端口都共享一条通道（k=1），测的是共享争用；拆到多通道才能逼近聚合峰值，且 HBM 的设计哲学正是「多端口各挂一条伪通道」。

## 7. 下一步学习建议

- **下一讲 u3-l4（写带宽变体）**：看同一框架下 `sp=` 行的端口名从 `in*` 换成 `out*`、缓冲标志换 `CL_MEM_WRITE_ONLY` 的对称实现，本讲的连接知识可直接平移。
- **横向对照多实例连接**：`ubench/streaming_bandwidth/datacenter/2ports_512bit/ubench.ini`（u4-l1 的素材）含 `stream_connect` 指令，是 `sp` 之外的第四种连接指令——内核端口连内核端口而非连内存。
- **看连接如何被自动生成**：`ubench/offchip_bandwidth/datacenter/auto_collect/connectivity_gen.py` 与 `hostcode_gen.py`（u5-l2 精读），你会看到本讲手工维护的「sp 行 ↔ flag」对齐关系如何被脚本用同一个参数源生成，从根本上消灭两端不一致。
- **看连接如何被用到极致**：`case_study/KNN/baseline_14PE`（u6-l1）把 14 个内核实例、每个多条端口铺满 U200 的 DDR 通道，`nk>1` 与多行 `sp=` 的规模化用法都在那里。
