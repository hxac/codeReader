# 二次开发实战：设计并集成你自己的微基准

## 1. 本讲目标

学完本讲，你应该能够：

1. 把「krnl_config.h + 内核 + host.cpp + ubench.ini + Makefile」五件套从**阅读对象**变成**可复用模板**：拷贝一个样板目录，按一份契约清单完成最小改造，得到一个能编译、能运行、能出数的新微基准。
2. 掌握「新参数贯穿」方法：为读写混合基准引入一个新参数（读、写两个数据量），并让它同时正确落到内核签名、s_axilite、setArg 编号、主机 payload 循环与带宽公式——五处落点一处都不能漏。
3. 把新基准接入 auto_collect 生成器族：在 `kernelcode_gen.py` 等四个生成器的 `if/elif` 分支结构中增加 `MIXED` 分支，使新基准也能被参数空间批量生成。
4. 按仓库「目录名即参数组合」的规范安放新目录，并仿照 datacenter README 的写法为新基准补全文档。

本讲是整套手册的收官实战之一：前面十几讲积累的所有机制（INTERFACE pragma、DATAFLOW、bank 绑定、连接配置、生成器契约）在这里被**同时**用在一次完整的二次开发里。

## 2. 前置知识

本讲不再重复讲基础机制，只列出必须已经掌握的内容（点击回看）：

| 前置概念 | 依赖讲义 | 一句话回顾 |
|---|---|---|
| 五件套骨架与「契约头」 | u1-l4 | krnl_config.h 被 v++ 与 g++ 两端共享，是全工程的参数单点 |
| m_axi / bundle / volatile / DATAFLOW | u2-l1、u3-l1 | bundle 异名＝独立端口；volatile 防死代码消除；DATAFLOW 让多个无依赖循环并发 |
| 主机 OpenCL 骨架与 bank 绑定 | u2-l2 | cl_mem_ext_ptr_t 的 flags 必须与 ini 的 sp 行逐端口对齐 |
| 带宽公式与计时口径 | u2-l3 | 魔数 `0.000010000 = NUM_ITERATIONS/1e9`；主机 chrono 计时含启动开销 |
| 写带宽变体的对称实现 | u3-l4 | 写内核无 DATAFLOW、用 `max_write_burst_length`、缓冲 `CL_MEM_WRITE_ONLY` 且不迁移 |
| auto_collect 四生成器 | u5-l2 | 生成器用「list 装字符串再 writelines」拼接，跨工具契约靠同组循环变量构造性对齐 |

本讲新引入的术语：

- **契约清单（contract checklist）**：五件套之间那些「改了一处就必须同步改另一处」的隐式约定。uBench 没有编译期检查保护这些约定，全靠人工对齐——所以二次开发的第一件事是把它们列成清单。
- **参数落点表**：一个新参数在「内核 / 主机 / ini / Makefile」四类文件中各自要改哪一行的映射表。
- **读写转向开销（bus turnaround）**：DDR 总线在读与写之间切换方向需要付出额外开销。读、写混合流量共享同一条通道时，实际带宽往往低于「纯读带宽与纯写带宽的折中」——这正是现有 read/write 两类基准**测不出来**、需要 mixed_rw 基准去填补的空白。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用法 |
|---|---|---|
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp` | 读带宽内核（双端口读样板） | 克隆母本之一：读循环、volatile 防优化 |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp` | 读版主机程序 | 克隆母本：bank 绑定、迁移、setArg、公式 |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h` | 契约头 | 直接复用，不改 |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile` | 构建脚本 | 克隆后**零修改**的候选 |
| `ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini` | 连接配置 | 克隆后补一行 sp |
| `ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp` | 写带宽内核 | 克隆母本之二：写循环、写突发 |
| `ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/ubench.ini` | 写版连接（nk=2 双实例） | 对照 nk 语义 |
| `ubench/offchip_bandwidth/datacenter/README.md` | 手动调参指南 + 目录规范来源 | 新基准 README 的仿写对象 |
| `ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py` | 内核生成器 | 增加 MIXED 分支的改造对象 |
| `ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py` | 主机生成器 | 需联动改造（setArg 编号） |
| `ubench/offchip_bandwidth/datacenter/auto_collect/connectivity_gen.py` | 连接生成器 | 需联动改造（sp 行） |
| `ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py` | 生成主脚本 | 目录命名与批跑入口 |
| `ubench/offchip_bandwidth/datacenter/auto_collect/config.py` | 参数空间 | ACCESS_TYPE 加 MIXED |

## 4. 核心概念与源码讲解

本讲的最小模块有三个：**4.1 五件套复用**、**4.2 新参数贯穿**、**4.3 auto_collect 集成**。三个模块对应二次开发的三步：先克隆出骨架，再注入新语义，最后纳入自动化。

### 4.1 五件套复用：从「读样板」到克隆契约清单

#### 4.1.1 概念说明

uBench 仓库里每一个手写微基准工程都是同一个模板的实例。证据就在 read 与 write 两个工程之间：它们的**差异极小且高度局部化**——内核一个读一个写、主机缓冲方向不同、ini 连线目标不同，而契约头、Makefile、主机骨架几乎逐字符相同。

「模板可复用」的准确含义不是「文件可以照抄」，而是：**改动只发生在固定的几个位置，其余部分原样保留即可工作**。把这些固定位置整理成清单，就是本模块要建立的「克隆契约清单」。二次开发时照单逐项核对，就不会出现「改了内核忘改 ini」这类运行期才暴露的错误。

为什么选 read/DDR/2ports_512bit 做母本？因为它是全仓库最简单的并发形态：**单内核（nk=1）× 双端口（DATAFLOW 并发）**，没有写版 nk=2 的多 CU 复杂度，也没有流版、延迟版的特殊机制。

#### 4.1.2 核心流程

克隆一个新基准目录的流程：

```text
1. 选母本        cp -r read/DDR/2ports_512bit mixed_rw/DDR/2ports_512bit
2. 核对深度      新目录与母本同深度 → Makefile 的 COMMON_REPO 不用动
3. 逐项核契约    C1..C6（见下表）
4. 改内核        换端口/换循环/加参数（4.2 节）
5. 改主机        换缓冲方向/换 NUM_* /换公式（4.2 节）
6. 改 ini        补/改 sp 行
7. 补 README     按 datacenter README 的五因素格式描述新基准
```

克隆时必须核对的六条契约：

| 编号 | 契约 | 内核侧 | 另一侧 | 违约后果 |
|---|---|---|---|---|
| C1 | 端口名与参数名硬绑定 | 内核签名 `in0/in1` | ini 的 `sp=krnl_ubench_1.in0:...` | 链接报端口找不到 |
| C2 | setArg 编号与参数顺序一致 | 签名从左到右 | host 的 `setArg(j, ...)`，指针参数必须在标量之前 | 参数错位，数据读错地址 |
| C3 | bank flag 与 sp 目标对齐 | — | host `flags = XCL_MEM_DDR_BANKn` ↔ ini `DDR[n]` | 数据落错通道，运行期暴露 |
| C4 | CU 名与 nk 一致 | — | host `"krnl_ubench_{i+1}"` ↔ ini `nk=krnl_ubench:1` | 创建 Kernel 失败 |
| C5 | 内核名贯穿三处 | `-k krnl_ubench` | Makefile 的 .xo 规则、host `krnl_name`、ini `nk/sp/slr` | 构建或运行失败 |
| C6 | 目录深度决定 COMMON_REPO | — | Makefile `COMMON_REPO = ../../../../../`（五级上溯到仓库根） | include 不到 common 库 |

其中 C6 最容易被忽略：Makefile 以相对路径回指仓库根的公共库（opencl.mk、xcl2.mk），**目录层级一变，相对路径就要跟着变**。

#### 4.1.3 源码精读

**（1）C6：COMMON_REPO 与目录深度。** 读样板工程的 Makefile 用五级 `../` 回到仓库根：

- [Makefile:L28-L31](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L28-L31)：`COMMON_REPO = ../../../../../` 定义相对锚点，并立即用 `readlink -f` 转成绝对路径 `ABS_COMMON_REPO`。

- [Makefile:L47-L54](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L47-L54)：`include $(ABS_COMMON_REPO)/common/includes/opencl/opencl.mk` 与 `xcl2.mk`，把公共库的编译/链接变量注入本工程——这就是 u7-l1 讲过的 `.mk 片段机制`的消费端。

新目录 `datacenter/mixed_rw/DDR/2ports_512bit` 与母本深度完全相同（`工程目录 → DDR → mixed_rw → datacenter → offchip_bandwidth → ubench → 仓库根`，同为五级上溯），所以 **Makefile 可以整体复用、一行不改**。

**（2）C5：内核名 krnl_ubench 的三处贯穿。**

- [Makefile:L81](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L81) 与 [Makefile:L95-L97](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L95-L97)：`.xo` 目标与 `v++ -c -k krnl_ubench` 编译规则硬编码内核名。
- [host.cpp:L39](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L39)：`std::string krnl_name = "krnl_ubench";`，随后在 [host.cpp:L73-L83](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L73-L83) 拼成 `"krnl_ubench:{krnl_ubench_1}"` 按 CU 名创建 Kernel 对象（C4）。
- [ubench.ini:L2-L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L2-L6)：`slr`/`sp`/`nk` 三条指令全部以 `krnl_ubench` 为主语。

结论：**新内核沿用 `krnl_ubench` 这个名字**是最省事的选择——Makefile、主机骨架、ini 模板全部免改。想改名（如 `krnl_mixed`）就要同时动上述三处，见 4.1.5 练习 2。

**（3）C3：bank 对齐的两个活例子。** 仓库自带一对正反教材：

- 读版：ini 把两个端口连到 `DDR[1]`（[ubench.ini:L3-L4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L3-L4)），主机两个分支（仿真/真机，内容其实相同，属模板遗留）都写 [XCL_MEM_DDR_BANK1](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L115-L128)。
- 写版：ini 连到 `DDR[0]`（[ubench.ini:L3-L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/ubench.ini#L3-L6)），主机 else 分支写 [XCL_MEM_DDR_BANK0](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L125-L131)。

两版各自「ini 的 n ↔ 主机的 BANKn」都对上了。克隆时选定一个通道（本讲选 DDR[0]），两端一起改。

**（4）C1/C2 将在 4.2 结合新参数细讲**，这里先记住证据位置：端口名出现在内核签名 [krnl_ubench.cpp:L4-L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L4-L6)，setArg 编号出现在 [host.cpp:L157-L163](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L157-L163)。

**（5）目录命名规范。** 仓库现有布局是 `datacenter/{read,write}/{DDR,HBM}/{ports}_{width}bit`（可用 `ls` 验证：`datacenter/` 下只有 `read`、`write`、`auto_collect`、`README.md`）。访问类型是第一级目录名，所以读写混合基准的规范位置是：

```text
ubench/offchip_bandwidth/datacenter/mixed_rw/DDR/2ports_512bit/
```

`2ports` 在这里的含义是「1 个读端口 + 1 个写端口，共 2 个内存端口」——与 u1-l2 讲过的「目录名只约束乘积」一致。

#### 4.1.4 代码实践

**实践目标**：完成新基准的克隆与契约审计，得到一个「除了待改的内核/主机/ini 外，其余部分已验证可用」的目录。

**操作步骤**：

1. 在 `ubench/offchip_bandwidth/datacenter/` 下执行：
   ```bash
   mkdir -p mixed_rw/DDR
   cp -r read/DDR/2ports_512bit mixed_rw/DDR/2ports_512bit
   cd mixed_rw/DDR/2ports_512bit
   ```
2. 清理构建产物：`make cleanall`（或直接删除 `_x.*`、`build_dir.*`、`sd_card*` 等生成物）。
3. 契约审计：逐条核对 C1–C6。重点验证 C6——在工程目录里执行 `readlink -f ../../../../../`，确认输出以仓库根结尾（能看到 `common/`、`ubench/`、`case_study/`）。
4. 用 `diff -r ../..../read/DDR/2ports_512bit/src .`（路径按实际）确认克隆件与母本的差异清单为空——此刻应当为空。

**需要观察的现象**：

- `readlink -f ../../../../../` 的输出路径；`diff -r` 无任何输出。

**预期结果**：

- C6 核对通过：五级上溯恰好落在仓库根，Makefile 无需修改即可 include 到 `common/includes/` 下的 .mk 片段。
- 至此新目录与母本逐字节一致，后续 4.2 的所有改动都建立在这份「干净克隆」上。

本实践不依赖 Vitis，纯文件操作即可完成。

#### 4.1.5 小练习与答案

**练习 1**：如果把新工程直接放在 `datacenter/` 根下（少两层目录），构建会坏在哪一行？怎么修？

**答案**：坏在 [Makefile:L29](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L29) 的 `COMMON_REPO = ../../../../../`——层级变浅后五级上溯越过仓库根，`ABS_COMMON_REPO` 指到仓库外，随后 [Makefile:L47-L48](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L47-L48) 的 `include` 找不到 `opencl.mk`/`xcl2.mk` 直接报错。修法：改成 `../../../`（三级）。auto_collect 生成版正是六层目录配六级 `../`（见 [makefile_gen.py:L38](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/makefile_gen.py#L38)）。

**练习 2**：把内核改名为 `krnl_mixed` 需要改动哪些文件的哪些行？

**答案**：至少三处——(1) Makefile 的 [.xo 目标与 -k 编译选项](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L81)（L81、L95）；(2) [host.cpp:L39](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L39) 的 `krnl_name`；(3) [ubench.ini:L2-L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L2-L6) 的 `slr/sp/nk` 主语。这就是为什么本讲建议**沿用 `krnl_ubench` 名字**，把改动预算留给真正的新语义。

**练习 3**：读版主机两个 bank 分支（`is_emulation()` 与 else）内容完全相同，写版却有 BANK1/BANK0 之差。哪个版本的处理更值得模仿？

**答案**：写版。它的 else 分支 [XCL_MEM_DDR_BANK0](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L125-L131) 与其 ini 的 `DDR[0]` 连线（[ubench.ini:L3-L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/ubench.ini#L3-L6)）真正对齐；读版两分支相同的 BANK1 只是碰巧与 DDR[1] 一致的模板遗留（u2-l2 已考证）。新基准应保证**每个分支**的 flag 都与 ini 一致，而不是依赖巧合。

### 4.2 新参数贯穿：给 mixed_rw 注入读写混合语义

#### 4.2.1 概念说明

现有 read/write 基准各测一个方向，但真实加速器几乎总是读写混流（KNN 读 searchSpace 写结果、SpMV 读矩阵写 y）。混合流量共享一条 DDR 通道时会触发**读写转向开销**，这个损耗纯读/纯写基准都测不到。mixed_rw 基准的目标就是补上这个维度。

设计决策：

- **端口配置**：1 个读端口 `in0`（bundle=gmem0）+ 1 个写端口 `out0`（bundle=gmem1），共 2 端口，与目录名 `2ports_512bit` 一致。
- **新参数**：内核签名增加**两个**标量 `size_rd`、`size_wr`（单位：宽字个数）。读写比例 \( r = \text{size\_wr}/\text{size\_rd} \) 由这两个参数之比定义——这就是「读写比例由 size 参数控制」的落点。
- **并发结构**：读循环与写循环无数据依赖，用 DATAFLOW 并发执行，测「同时读+写」的通道行为。
- **防优化**：读侧沿用 volatile 临时变量；写侧本身是不可消除的副作用。

「新参数贯穿」是本模块的方法论核心：一个新参数不是加一行代码，而是**沿契约链走一遍**，每个落点都要对上。

#### 4.2.2 核心流程

mixed_rw 内核的执行模型：

```text
krnl_ubench(in0, out0, size_rd, size_wr)
├── DATAFLOW 并发两个进程
│   ├── 进程 R：for i in 0..NUM_ITERATIONS: for j in 0..size_rd: temp = in0[j]   (volatile 消费)
│   └── 进程 W：for i in 0..NUM_ITERATIONS: for j in 0..size_wr: out0[j] = 常量
└── return
```

主机的参数落点表（本讲最重要的交付物）：

| 参数 | 内核落点 | 主机落点 | ini 落点 | Makefile 落点 |
|---|---|---|---|---|
| `size_rd`（新） | 签名第 3 参 + `s_axilite` | `setArg(2, …)`；读缓冲按 payload_rd 分配 | 无（标量对 ini 透明） | 无 |
| `size_wr`（新） | 签名第 4 参 + `s_axilite` | `setArg(3, …)`；写缓冲按 payload_wr 分配 | 无 | 无 |
| 读写比 \( r \) | 无（由两 size 之比隐式定义） | `payload_wr = payload_rd * r` | 无 | 无 |
| 端口位宽 | `krnl_config.h` 的 DWIDTH | `dataSize / WIDTH_FACTOR` 换算 | 无 | 无 |
| 突发长度 | `max_read/write_burst_length` pragma | 无（透明） | 无 | 无 |
| 内存通道 | 无 | `flags = XCL_MEM_DDR_BANK0` | `sp=…:DDR[0]` | 无 |
| 内核频率 | 无 | 无 | 无 | CLFLAGS（仅 auto_collect 生成版有 `--kernel_frequency`） |

带宽公式推导。设 `payload_rd`、`payload_wr` 为以 32bit int 计的数据量，每次内核调用搬移的总字节数为 \( (\text{payload}_{rd} + \text{payload}_{wr}) \times 4 \)，重复 NUM_ITERATIONS 次：

\[
\text{BW} = \frac{(\text{payload}_{rd} + \text{payload}_{wr}) \times 4 \times \text{NUM\_ITERATIONS}}{t \times 10^{9}} \quad (\text{GB/s})
\]

代入魔数 `0.000010000 = 10000/10⁹ = NUM_ITERATIONS/10⁹` 并令 \( r = \text{payload}_{wr}/\text{payload}_{rd} \)：

\[
\text{BW} = \frac{\text{payload}_{rd} \times 4 \times 0.000010000 \times (1+r)}{t} \times \text{NUM\_KERNEL}
\]

**自检**：当 \( r = 1 \) 且两个端口各搬 payload 字节时，系数 \( (1+r) = 2 \) 恰好等于活跃端口数——退化为读版公式 `payload*4*0.000010000/t*NUM_KERNEL*NUM_PORT`（NUM_PORT=2）的形式。这与 u3-l4 的结论「公式系数 = 真实并发通道数」完全一致，只是现在系数来自**逐端口流量求和**而非等流量假设，是更一般的写法。

#### 4.2.3 源码精读

mixed_rw 的内核与主机是「示例代码」——由仓库两个真实内核拼接改造而来，不是仓库原有文件：

**（1）内核：从读版取循环骨架，从写版取写语句。**

读版的双端口读循环（母本 A）：

- [krnl_ubench.cpp:L4-L6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L4-L6)：签名 `krnl_ubench(in0, in1, size)`，两个 m_axi 端口各占一个异名 bundle（gmem0/gmem1），`max_read_burst_length=16`。
- [krnl_ubench.cpp:L14-L17](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L14-L17)：两个 volatile 临时变量（每循环独占一个，是 DATAFLOW 无依赖的前提）+ DATAFLOW。
- [krnl_ubench.cpp:L19-L24](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_ubench.cpp#L19-L24)：读循环模板，`temp_data_0 = in0[j]` 配 II=1。

写版的写循环（母本 B）：

- [write/krnl_ubench.cpp:L4-L5](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp#L4-L5)：写端口 pragma 用 `max_write_burst_length=16`。
- [write/krnl_ubench.cpp:L11-L18](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/krnl_ubench.cpp#L11-L18)：`volatile INTERFACE_WIDTH temp_data_0 = 100;` 初值常量，循环体 `out0[j] = temp_data_0;`。

拼接后的 mixed_rw 内核（示例代码）：

```c++
#include "krnl_config.h"

extern "C" {
    void krnl_ubench(volatile INTERFACE_WIDTH* in0, volatile INTERFACE_WIDTH* out0,
                     const int size_rd, const int size_wr) {
#pragma HLS INTERFACE m_axi port=in0  offset=slave bundle=gmem0 max_read_burst_length=16
#pragma HLS INTERFACE m_axi port=out0 offset=slave bundle=gmem1 max_write_burst_length=16
#pragma HLS INTERFACE s_axilite port=in0 bundle=control
#pragma HLS INTERFACE s_axilite port=out0 bundle=control
#pragma HLS INTERFACE s_axilite port=size_rd bundle=control
#pragma HLS INTERFACE s_axilite port=size_wr bundle=control
#pragma HLS INTERFACE s_axilite port=return bundle=control

        volatile INTERFACE_WIDTH temp_data_rd;          // 消费读值，防死代码消除
        volatile INTERFACE_WIDTH temp_data_wr = 100;    // 写侧初值常量

#pragma HLS DATAFLOW
        for (int i = 0; i < NUM_ITERATIONS; i++) {
            for (int j = 0; j < size_rd; j++) {
#pragma HLS PIPELINE II=1
                temp_data_rd = in0[j];
            }
        }
        for (int i = 0; i < NUM_ITERATIONS; i++) {
            for (int j = 0; j < size_wr; j++) {
#pragma HLS PIPELINE II=1
                out0[j] = temp_data_wr;
            }
        }
        return;
    }
}
```

与母本的差异只有三处：第二个指针参数由 `in1` 换成 `out0`（bundle 名 gmem1 保留，仍是独立端口）；读/写 pragma 分别取自母本 A/B；`size` 拆成 `size_rd`/`size_wr` 两个标量。契约头 `krnl_config.h` 原样复用（[DWIDTH/WIDTH_FACTOR/NUM_ITERATIONS](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/krnl_config.h#L4-L7) 一字不改）。

**（2）主机：C2 契约（setArg 编号）的证据与改法。**

读版主机的参数设置循环：

- [host.cpp:L155-L163](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L155-L163)：先把 `dataSize` 除以 `WIDTH_FACTOR` 换算成宽字个数，然后**按内核签名顺序**先设 NUM_PORT 个缓冲（`setArg(j, …)`，j 从 0 递增），再设 `setArg(j, dataSize)`，最后 `enqueueTask`。指针参数在前、标量在后的顺序就在这里体现。

mixed_rw 的对应段（示例代码）：

```c++
int size_rd = dataSize / WIDTH_FACTOR;          // dataSize 仍是 int 个数
int size_wr = size_rd * RATIO;                  // RATIO 取 1/2/4
OCL_CHECK(err, err = cmpt_krnl[0].setArg(0, source_in_buffer));   // in0
OCL_CHECK(err, err = cmpt_krnl[0].setArg(1, source_out_buffer));  // out0
OCL_CHECK(err, err = cmpt_krnl[0].setArg(2, size_rd));
OCL_CHECK(err, err = cmpt_krnl[0].setArg(3, size_wr));
OCL_CHECK(err, err = q.enqueueTask(cmpt_krnl[0]));
q.finish();
```

写缓冲要按 `max(size_rd, size_wr)` 个宽字分配，避免 ratio>1 时越界。

**（3）主机：缓冲方向的两个母本证据。**

- 读版缓冲：[CL_MEM_READ_ONLY](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L133-L140) 且随后 [enqueueMigrateMemObjects](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L142-L145) 把数据搬到设备（读之前必须有数据）。
- 写版缓冲：[CL_MEM_WRITE_ONLY](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L136-L145) 且**没有**迁移调用（写方向无需预置数据）。

mixed_rw 各取一半：`in0` 缓冲 READ_ONLY + 迁移，`out0` 缓冲 WRITE_ONLY + 不迁移。bank flag 按 4.1.3 第 (3) 点的决策统一为 `XCL_MEM_DDR_BANK0`（两个分支都写）。

**（4）ini：sp 行补一条。** 读版 ini 的 [三条连接指令](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/ubench.ini#L2-L6) 改成（示例代码）：

```ini
[connectivity]
slr=krnl_ubench_1:SLR0
sp=krnl_ubench_1.in0:DDR[0]
sp=krnl_ubench_1.out0:DDR[0]

nk=krnl_ubench:1
```

端口名 `in0`/`out0` 必须与内核签名逐字符一致（C1）。两个端口先都连 DDR[0]——**同通道读写争用**正是本基准要测的对象；拆到 DDR[0]/DDR[1] 的对照实验见 4.2.5 练习 2。

**（5）公式落点。** 读版公式在 [host.cpp:L172-L173](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L172-L173)（写版 [L171-L172](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/write/DDR/2ports_512bit/src/host.cpp#L171-L172) 逐字符相同）。mixed_rw 按 4.2.2 的推导改为（示例代码）：

```c++
double bw_result = payload_rd * 4 * 0.000010000 * (1 + RATIO) / kernel_time_in_sec * NUM_KERNEL;
std::cout << "RD:WR = 1:" << RATIO << " - Bandwidth = " << bw_result << "GB/s" << std::endl;
```

注意顺手修掉母本的 payload 打印 bug（[L173](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L173) 打的 `i*4/…` 与 payload 无关，u2-l3 已考证），新代码直接打印设定的比例。

#### 4.2.4 代码实践

**实践目标**：完成 mixed_rw 的内核、主机、ini 三件改造，让克隆目录成为一个语义完整的新基准；有 Vitis 环境时用 sw_emu 验证功能链路。

**操作步骤**：

1. **改内核**：用 4.2.3 (1) 的示例代码整体替换 `src/krnl_ubench.cpp`。
2. **改 ini**：用 4.2.3 (4) 的示例内容替换 `ubench.ini`（注意 sp 目标统一 DDR[0]）。
3. **改主机**：在 `src/host.cpp` 中——
   - 顶部改为 `#define NUM_KERNEL 1`、`#define NUM_PORT 2`（沿用母本值，见 [L15-L16](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L15-L16)），另加 `#define RATIO 1`；
   - 保留 payload 倍增循环（[L100](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/src/host.cpp#L100)）作为 `payload_rd`；`payload_wr = payload_rd * RATIO`；
   - 分配两个缓冲：读缓冲 `payload_rd` 个 int、READ_ONLY + ext_ptr(BANK0) + 迁移；写缓冲 `max(payload_rd, payload_wr)` 个 int、WRITE_ONLY + ext_ptr(BANK0) + 不迁移；
   - setArg 段替换为 4.2.3 (2) 的四行版本；
   - 公式与打印替换为 4.2.3 (5) 的版本。
4. **验证**（需 Vitis + XRT 环境）：`make check TARGET=sw_emu DEVICE=<平台名>`；无 Vitis 时跳过运行，只做下一步静态核对。

**需要观察的现象**：

- sw_emu 下程序逐设备打印 `Trying to program device…`，随后每个 payload 档输出一行 `Execution time` 与一行 `RD:WR = 1:x - Bandwidth = …`。
- 报错形态（如果契约没对齐）：sp 行端口名拼错 → 链接期报 port not found；setArg 编号错位 → 运行期内核拿到错误 size 或缓冲；flag 与 sp 不一致 → 数据通道错乱、仿真中可能表现为读回全零。

**预期结果**：

- 功能正确性：sw_emu 跑完 11 档 payload 不崩溃（数值无物理意义，u1-l3 已说明）。
- 公式自检：RATIO=1 时带宽公式数值 = 母本公式在 NUM_PORT=2 下的数值（同为 `payload*4*0.000010000*2/t`），可用来验证改写没有引入系数错误。
- 本环境的运行结果：**待本地验证**（本讲义撰写环境无 Vitis，无法实际执行 make）。

#### 4.2.5 小练习与答案

**练习 1**：把 RATIO 从 1 改成 4，内核侧代码需要改几行？主机侧呢？

**答案**：内核侧 0 行——`size_wr` 由主机传入，比例对内核只是两个循环边界之比；主机侧改 1 行（`RATIO` 宏）。这正是「比例由 size 参数控制」设计的好处：比例扫描完全在主机侧完成，不用重新综合内核（hw 综合一次要数小时）。

**练习 2**：若把 `out0` 改连 `DDR[1]`（`sp=krnl_ubench_1.out0:DDR[1]`），公式和测量含义各有什么变化？

**答案**：公式不变——它只数「每秒搬移的总字节」，与端口接哪条通道无关（前提是每个端口的流量仍被 `(1+r)` 正确计入）。测量含义变了：同通道（都连 DDR[0]）测的是**读写争用与转向开销**，混合带宽通常低于纯读、纯写的线性组合；跨通道（in0→DDR[0]、out0→DDR[1]）测的是**读通道 + 写通道的聚合上限**。同时主机侧 `source_out_ext[i].flags` 必须同步改成 `XCL_MEM_DDR_BANK1`（C3 契约）。两种配置各跑一遍，差值就是转向开销的量化估计。

**练习 3**：另一种混合设计是单循环 copy 内核：`for (j…) { temp = in0[j]; out0[j] = temp; }`。它与本讲的双循环 DATAFLOW 设计测的有什么不同？

**答案**：copy 内核里读写发生在**同一进程同一条 II=1 流水线**上，读到的值必须写走，形成数据依赖链，更接近 DMA 搬运的访问模式；双循环 DATAFLOW 设计里读、写是**两个独立并发进程**，各自全速压满端口，测的是通道在并发混流下的极限。两者的数值差异本身就是有价值的数据。copy 变体还能自然消费读值（写就是消费），不需要额外防优化技巧。

### 4.3 auto_collect 集成：让生成器认识 MIXED

#### 4.3.1 概念说明

手工基准只能覆盖参数空间的一个点；要扫比例、扫突发、扫位宽的交叉积，必须把 mixed_rw 纳入 auto_collect。u5-l1/l2 已经讲过生成器族的架构：`config.py` 定义参数空间，`generate_microbenchmarks.py` 六层嵌套循环为每个组合调用四个生成器产出五件套。

本模块的关键观察是：**四个生成器对 `access_type` 的处理全是 `if 'RD' … elif 'WR' … else 报错` 的分支结构**。这意味着接入一种新访问类型不需要动架构——只需在每个分支点上增加第三个分支。这是典型的「开闭原则」缺口：生成器对扩展开放（加分支），但对修改也开放（必须改四个文件），没有插件机制。

需要改动的文件清单：

| 文件 | 改动 | 原因 |
|---|---|---|
| `config.py` | `ACCESS_TYPE` 加 `'MIXED'` | 参数空间多一维取值 |
| `kernelcode_gen.py` | 4 个分支点加 elif | 签名/pragma/临时变量/循环体 |
| `hostcode_gen.py` | 2 个分支点加 elif | 缓冲分配/setArg 语句 |
| `connectivity_gen.py` | 1 个分支点加 elif | sp 行的端口名 |
| `generate_microbenchmarks.py` | 不改 | 目录名直接拼接 `access_type`，MIXED 自动流入 |
| `makefile_gen.py` | 不改 | 唯一参数是频率 |

#### 4.3.2 核心流程

生成一个 MIXED 设计的调用链：

```text
config.py: ACCESS_TYPE 含 'MIXED'
   ↓
generate_microbenchmarks.py 六层循环命中 access_type='MIXED'
   ├─ 目录名 = 'MIXED_' + 'DDR' + '_300MHz_2port_512bit_16max_burst_length'   (L29-32 拼接)
   ├─ generateMakefile(kernel_freq)                                          → Makefile
   ├─ generateConnectivity('MIXED', num_port, bank_name)                     → ubench.ini
   ├─ generateHostCode('MIXED', num_port, port_width, start, stop, bank_flag) → host.cpp
   └─ generateKernelCode('MIXED', num_port, port_width, max_burst)           → krnl_ubench.cpp + krnl_config.h
   ↓
runAll.sh 追加一行 make check TARGET=hw DEVICE=…                             (L60-61)
```

mixed 生成器的语义约定（与 4.2 手工版对齐）：`num_concurrent_port` 个端口对半分为读、写两组（奇数时读组多一个）；签名生成 `in0…in_{p-1}, out0…out_{q-1}, size_rd, size_wr`；比例不进目录名（固定 1:1），扫描比例属于未来扩展维度。

#### 4.3.3 源码精读

**（1）kernelcode_gen.py 的四个分支点。**

- 分支点 1（签名）：[kernelcode_gen.py:L34-L44](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L34-L44)——按端口循环，`RD` 生成 `in<N>`、`WR` 生成 `out<N>`，最后追加 `const int size` 与 `int* sum`。MIXED 分支在此生成 `in0, out0` 两个指针和 `size_rd, size_wr` 两个标量（注意保持指针在前、标量在后的顺序，保护 setArg 契约）。
- 分支点 2（pragma）：[kernelcode_gen.py:L46-L59](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L46-L59)——`RD` 配 `max_read_burst_length`（L48）、`WR` 配 `max_write_burst_length`（L51）；L57-L58 是生成版特有的 `sum` 写回口（消费读值防优化的替代方案，u5-l2 已讲）。
- 分支点 3（临时变量）：[kernelcode_gen.py:L64-L74](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L64-L74)——RD 临时变量（L66）与 WR 带初值的临时变量（L68）。**注意 L66 生成的声明缺分号**（`temp_data_N` 而非 `temp_data_N;`），这是 u5-l2 确证过的必致编译失败的脚本缺陷——新的 MIXED 分支必须带上分号，别把 bug 一起继承。
- 分支点 4（测量循环）：[kernelcode_gen.py:L75-L94](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L75-L94)——RD 循环体在 L80-L82（读 + range 截取 + 累加），WR 循环体在 L89-L90（写 + 累加）。MIXED 分支直接复用这两段循环体，分别以 `size_rd`、`size_wr` 为边界。

契约头生成器 [kernelcode_gen.py:L6-L21](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/kernelcode_gen.py#L6-L21) 只含 DWIDTH/INTERFACE_WIDTH/NUM_ITERATIONS（生成版没有 WIDTH_FACTOR，换算内联进主机），对访问类型不敏感，MIXED 无需改动。

**（2）hostcode_gen.py 的两个分支点与编号联动。**

- 分支点 1（缓冲分配）：[hostcode_gen.py:L103-L148](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L103-L148) 是 RD 分支（含迁移），[L148-L187](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L148-L187) 是 WR 分支（不迁移）。MIXED 分支要两组缓冲各按母本拼出。payload 循环边界在 [L95](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L95) 由 CONSECUTIVE_DATA_SIZE 换算（`KB*1024/4` 个 int），对 MIXED 应生成 `payload_wr = payload_rd * RATIO` 一行。
- 分支点 2（setArg 语句）：[hostcode_gen.py:L205-L214](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L205-L214)——L206/L208 按 access_type 选缓冲变量名，L213 设 `size`，L214 设 `sum` 缓冲。**这是联动改动的关键**：MIXED 内核签名多了一个 `size_wr`，这里必须生成两个标量 setArg（`j` 与 `j+1`），随后 `sum` 的编号顺移到 `j+2`。u5-l2 讲过的「跨工具契约由同组循环变量构造性对齐」在这里接受考验：内核签名生成循环（kernelcode_gen L34-44）与 setArg 生成段（hostcode_gen L205-214）必须产出**同一参数表**。
- 位宽换算的落点在 [hostcode_gen.py:L200](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L200)：`dataSize / (port_width/32)`——MIXED 版对 `size_rd`、`size_wr` 各做一次。

**（3）connectivity_gen.py 的分支点。**

- [connectivity_gen.py:L13-L17](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/connectivity_gen.py#L13-L17)：按端口循环生成 sp 行，RD 用 `.in<N>`（L15）、WR 用 `.out<N>`（L17）。MIXED 分支生成两组 sp 行（读组、写组各指到 bank_name）。`slr` 行（L11）与 `nk` 行（L21）与访问类型无关，免改。

**（4）config.py 与主脚本。**

- [config.py:L12](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/config.py#L12)：`ACCESS_TYPE = ['RD', 'WR']` → 加 `'MIXED'`。
- [generate_microbenchmarks.py:L22-L27](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L22-L27)：六层嵌套循环遍历 `ACCESS_TYPE`，无需感知新取值；[L29-L32](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L29-L32) 的目录名拼接把 `access_type` 放在最前，自动得到 `MIXED_DDR_300MHz_…`；[L60-L61](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L60-L61) 把每个设计追加进 runAll.sh。

**（5）Python 版本问题（实测结论）。** `kernelcode_gen.py` 全部 `print` 都带括号（L40、L54、L70、L94）、无整除运算，**可以直接在 Python 3 下 import 使用**；`connectivity_gen.py` 同样干净。真正卡 Python 3 的只有主脚本 [generate_microbenchmarks.py:L91](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L91) 的裸 `print` 和 `hostcode_gen.py` [L95](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/hostcode_gen.py#L95) 的 `/` 整除（Python 3 下变浮点，会把 `256.0` 拼进 C 代码）。所以**先单测 mixed 生成器（Python 3 即可），再迁移主脚本**是最省力的顺序。

#### 4.3.4 代码实践

**实践目标**：写出 `mixed_kernelcode_gen.py`，使 `generateKernelCode('MIXED', …)` 能产出与 4.2 手工版等价的内核源码，并用 diff 验证等价性。

**操作步骤**：

1. 复制生成器：`cp auto_collect/kernelcode_gen.py auto_collect/mixed_kernelcode_gen.py`。
2. 在四个分支点各加一个 `elif (access_type == 'MIXED')` 分支（对照 4.3.3 (1) 的行号定位）：
   - 签名：生成 `in0, out0, const int size_rd, const int size_wr, int* sum`；
   - pragma：`in0` 用 `max_read_burst_length`、`out0` 用 `max_write_burst_length`（值取参数 `max_burst_length`）；
   - 临时变量：`temp_data_rd`（**带分号**）、`temp_data_wr = 100;`，再加 `temp_sum_rd/temp_sum_wr` 供 sum 写回；
   - 循环：RD 循环体（L77-L84 的模板）以 `size_rd` 为界，WR 循环体（L85-L92 的模板）以 `size_wr` 为界，末尾 `sum[0]=…; sum[1]=…;`。
3. 单元测试（Python 3 即可，见 4.3.3 (5) 的依据）：
   ```bash
   cd auto_collect && mkdir -p /tmp/mixed_test && cd /tmp/mixed_test
   python3 -c "
   import sys; sys.path.insert(0, '<auto_collect 绝对路径>')
   from mixed_kernelcode_gen import generateKernelCode
   generateKernelCode('MIXED', 2, 512, 16)"
   ```
4. 对比验证：`diff krnl_ubench.cpp <(4.2 手工版去掉 sum 口后的内核)`，逐行解释差异（预期差异只剩 sum 写回口与相关累加语句——生成版用 sum 消费读值，手工版用 volatile 消费，两种防优化手段等价目标不同实现）。

**需要观察的现象**：

- 生成的 `krnl_ubench.cpp` 中签名顺序为「指针在前、标量在后」；两段循环分别以 `size_rd`/`size_wr` 为边界；`temp_data_rd` 声明带分号。
- diff 输出集中在 sum 相关行，核心循环结构与手工版一致。

**预期结果**：

- `python3 -c` 调用无语法错误，产出两个文件（`krnl_ubench.cpp` 与 `krnl_config.h`）。
- 本环境已核实 `kernelcode_gen.py` 无 Python 2 专有语法，故此步**不需要**迁移即可在 Python 3 运行；但把它接回 `generate_microbenchmarks.py` 全流程仍需先处理主脚本的裸 print（L91）——完整批跑**待本地验证**。
- 生成代码能否通过 v++ 综合：**待本地验证**（需 Vitis 环境）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `generate_microbenchmarks.py` 一行不改就能让新目录名带上 MIXED 前缀？

**答案**：目录名在 [L29-L32](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/auto_collect/generate_microbenchmarks.py#L29-L32) 由 `'access_type + '_' + memory_type['BANK_TYPE'] + …'` 字符串拼接而成，循环变量直接流入名字；`ACCESS_TYPE` 列表加一个元素，循环就多迭代一轮、名字自动带 `MIXED_` 前缀。代价是目录数按 ACCESS_TYPE 长度**翻倍**（u5-l1 的连乘纪律：改配置前先推演目录总数）。

**练习 2**：只改 `kernelcode_gen.py` 不改 `hostcode_gen.py`，会发生什么？

**答案**：生成的内核签名有 5 个参数（in0、out0、size_rd、size_wr、sum），而主机仍按旧模板 setArg：设了 2 个缓冲、1 个 `size`（值还是 `dataSize/(port_width/32)`，语义已错）、1 个 `sum`——参数个数与类型都对不上，轻则 `clSetKernelArg` 报错，重则 size_rd 拿到错值、size_wr 读到垃圾导致越界访问。这就是「跨工具契约」没有编译期保护的实例，也是 4.3.3 (2) 强调编号联动的原因。

**练习 3**：如果想让 auto_collect 扫描读写比例（1:1、1:2、1:4 各生成一个目录），最小改动方案是什么？

**答案**：在 `config.py` 加一维 `MIX_RATIO = [1, 2, 4]`；在 `generate_microbenchmarks.py` 的循环里对 `access_type == 'MIXED'` 的组合内嵌一层 `for ratio in MIX_RATIO`（或统一加一层循环、非 MIXED 时列表取 `[1]`），并把 ratio 拼进 `benchmarkDesignName`（如 `MIXED_DDR_300MHz_2port_512bit_16burst_r2`）；`generateKernelCode`/`generateHostCode` 增加 ratio 参数用于生成 `size_wr = size_rd * ratio`（内核源码不变，只需主机侧乘系数）。目录数变化：MIXED 类目录数 × len(MIX_RATIO)。注意 `os.mkdir` 使主脚本不可重复执行（u5-l1），改配置前先清掉旧的 `uBenchDesignDir`。

## 5. 综合实践

**任务：交付完整的 mixed_rw 基准，并用它量化 DDR 同通道读写混合的带宽损耗。**

把三个模块串成一条流水线：

1. **克隆与审计**（4.1）：`mixed_rw/DDR/2ports_512bit` 就位，C1–C6 契约逐条核对通过，Makefile 零修改。
2. **语义注入**（4.2）：内核（in0 读 + out0 写 + size_rd/size_wr）、主机（双缓冲按方向分配、四参数 setArg、`(1+r)` 公式）、ini（两条 sp 行同连 DDR[0]）全部就位。
3. **自动化**（4.3）：`mixed_kernelcode_gen.py` 通过单元测试，diff 证明与手工版等价（进阶，可选）。
4. **测量与报告**（需 U200/U280 真机；无硬件则完成 1–3 与报告框架，数值标注待本地验证）：
   - 对 RATIO ∈ {1, 2, 4} 各跑一遍 payload 扫描（256→262144），记录每档 `BW_mixed`；
   - 跑母本 read、write 两个基准取得同 payload 档的 `BW_rd`、`BW_wr`；
   - 计算混合效率 \( \eta = \dfrac{\text{BW}_{mixed}}{\text{BW}_{rd} \cdot \frac{1}{1+r} + \text{BW}_{wr} \cdot \frac{r}{1+r}} \)，即实测混合带宽相对「按流量加权的纯方向带宽」的比值——\(\eta < 1\) 的部分就是读写转向与通道争用的损耗；
   - 对照实验：把 `out0` 改连 DDR[1]（ini sp 行 + 主机 BANK1 两处联动）重跑，比较同通道与跨通道的 \(\eta\)。
5. **补全 README**：仿照 [datacenter/README.md:L2](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/README.md#L2) 的五因素格式，说明 mixed_rw 测什么（读写混合带宽）、目录名含义（2ports = 1 读 + 1 写）、比例怎么改（RATIO 宏）、ini 两端联动规则（C3）。

**交付物清单**：新目录（五件套 + README）、契约审计表（C1–C6 逐项打勾）、三种比例的带宽数据表、混合效率 \(\eta\) 曲线、以及一段结论——同通道混流损耗是否显著、跨通道拆分能挽回多少。这份报告同时就是 u6-l4 方法论的一次反向应用：用微基准数据回答一个设计问题（「我的读写混流内核该共享通道还是拆通道？」）。

## 6. 本讲小结

- **五件套是模板不是文档**：read/write 两工程的差异高度局部化，证明克隆 + 契约清单（C1 端口名、C2 setArg 编号、C3 bank 对齐、C4 CU 名、C5 内核名、C6 目录深度/COMMON_REPO）就是可靠的二次开发路径。
- **新参数要沿契约链走完全程**：`size_rd`/`size_wr` 这对新标量同时落点于内核签名、s_axilite、setArg 编号、payload 循环与带宽公式；参数落点表是防止漏改的工具。
- **公式系数 = 逐端口流量之和**：mixed_rw 的 \( (1+r) \) 系数是读版 `×NUM_PORT` 的一般化，等流量时二者等价——改任何带宽公式前先重推系数，不要复制魔数。
- **生成器族的扩展点就是分支点**：四个 `*_gen.py` 共 7 个 `if/elif` 分支点各加一个 MIXED 分支即可接入 auto_collect；主脚本靠字符串拼接自动吸收新类型，但目录数按参数空间连乘增长。
- **改造时机也是修复时机**：新分支别继承母版的缺陷——RD 临时变量缺分号（kernelcode_gen L66）、payload 打印 bug（host L173）、两分支相同的 bank flag 模板遗留，都应在 mixed_rw 中一并修正。

## 7. 下一步学习建议

- **u7-l3 测量方法学批判、性能剖析与仓库已知问题**：mixed_rw 的数字要可信，必须先弄清主机 chrono 计时的系统性误差与 xrt.ini 内核级剖析的替代方案——本讲的 `make check` 流程末尾已经悄悄调用了 `perf_analyze`（[Makefile:L133-L135](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/read/DDR/2ports_512bit/Makefile#L133-L135)），下一讲解释它。
- **回看 u6-l4**：本讲综合实践算出的混合效率 \(\eta\) 正是「带宽利用率」方法在读写混流场景的直接应用；可尝试把 mixed_rw 的结论反哺到 KNN/SpMV 的通道分配决策上。
- **扩展阅读**：`ubench/streaming_bandwidth/datacenter/auto_collect/` 的生成器族是另一套「接入新维度」的实例（它砍掉了突发维度、新增 NUM_KERNEL），对照阅读能加深对本讲分支点扩展模式的理解。
- **源码阅读建议**：动手前重读 [datacenter/README.md](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/README.md#L48-L52) 的第 4、5 节——payload 扫描与连接配置是 mixed_rw 改造中出错率最高的两处。
