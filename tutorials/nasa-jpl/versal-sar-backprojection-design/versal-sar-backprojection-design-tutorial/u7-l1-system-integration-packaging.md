# 系统集成：system.cfg、XSA 链接与打包

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `design/system_cfgs/system.cfg` 这个文件里 `nk=`、`stream_connect=`、`sp=` 三类指令各自的含义，以及 Makefile 是如何从 `design/common.h` 读出 `AIE_SWITCHES` 自动生成它们的。
- 描述 `v++ -g -l` 如何把 AIE 的 `libadf.a` 与 PL 的 `dma_pkt_router.xo` 链接成一张完整的硬件设计（XSA）。
- 列出 `v++ -p` 打包 SD 卡镜像时用到的各类 `--package` 选项，并解释每样东西（DTB、u-boot、内核、rootfs、host elf、xclbin）各自的角色。
- 理解为什么 `system.cfg` 是构建产物而非源码，以及 `.gitkeep` 在这里的作用。

本讲是「系统集成与硬件部署」单元的第一讲，把前面单元里分别讲过的 AIE 图拓扑（u4）、PL 包路由器（u6）和构建系统（u1-l3）在「编译 → 链接 → 打包」这条主线上缝合起来。

## 2. 前置知识

在进入本讲前，请确认你已经理解下面这些前置概念（来自依赖讲义）：

- **三域分工与构建变量**（u1-l3）：项目用 `TARGET`（hw / hw_emu / sw_emu）与 `PLATFORM` 两个全局变量驱动 Makefile；AIE 源码经 `v++ --mode aie` 生成 `libadf.a`，PL 源码经 `v++ --mode hls` 生成 `.xo`，二者都是「半成品」。
- **AIE 图的 PLIO 端口命名**（u4-l3）：每个 switch 子图有一个 `output_plio` 端口 `plio_pkt_rtr_out`，其名字由两个实例计数器拼成：`plio_pkt_rtr_out_<bp_graph_insts>_<bp_subgraph_insts>`。顶层图只有一张，故 `bp_graph_insts` 恒为 0；子图（switch）有 `AIE_SWITCHES` 个，故端口名形如 `plio_pkt_rtr_out_0_0`、`plio_pkt_rtr_out_0_1`、…、`plio_pkt_rtr_out_0_6`。
- **PL 包路由器的接口**（u6-l1）：`dma_pkt_router` 这个 HLS 内核有两个对外端口——`pl_stream_in`（128 位 AXI4-Stream 输入，接 AIE 的 PLIO）和 `ddr_mem`（`m_axi` 主写口，把重排后的图像写回 DDR）。本讲要做的「接线」，就是把 AIE 的 PLIO 输出连到 `pl_stream_in`，把 `ddr_mem` 连到 DDR。
- **`AIE_SWITCHES` 宏**（u1-l4）：在 `design/common.h` 中定义为 7，决定有多少个 switch 子图、多少个 PLIO 输出端口、多少个 PL 包路由器实例。

如果你对这些还不熟，建议先回去读对应讲义；本讲会把它们当成已知结论来用。

## 3. 本讲源码地图

本讲主要围绕下面几个文件：

| 文件 | 作用 |
|------|------|
| [Makefile](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile) | 总指挥：自动生成 system.cfg、链接 XSA、打包 SD 卡镜像，三条逻辑全在这里。 |
| [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) | 提供 `AIE_SWITCHES`、`RC_SAMPLES` 等宏，Makefile 用 `grep` 把它们抠出来驱动生成。 |
| [design/pl/pkt_router_config.cfg](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/pkt_router_config.cfg) | PL 内核的 HLS 编译配置（时钟、顶层函数、输出 xo 格式）。 |
| [design/system_cfgs/.gitkeep](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/system_cfgs/.gitkeep) | 占位文件，让 Git 保留住这个空目录——真正的 `system.cfg` 是构建产物，被 `.gitignore` 忽略。 |
| [design/aie/graph.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h) | PLIO 端口的「另一端」，其命名必须与 system.cfg 里的 `stream_connect` 严格对齐。 |
| [design/pl/dma_pkt_router.cpp](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp) | PL 内核的 `pl_stream_in` / `ddr_mem` 端口定义，是 `stream_connect` / `sp` 的连接终点。 |

整条流水线可以概括为一句话：**编译（`v++ -c`）产出两个「对象文件」→ 链接（`v++ -g -l`）用 system.cfg 把它们焊成一片硬件（XSA）→ 打包（`v++ -p`）把硬件和软件塞进一张可启动的 SD 卡镜像。** 这和传统编译器 `编译 → 链接 → 生成可执行映像` 的隐喻高度对应，后面会反复用到。

## 4. 核心概念与源码讲解

### 4.1 system.cfg 的自动生成：nk / stream_connect / sp

#### 4.1.1 概念说明

AIE 图（`libadf.a`）和 PL 内核（`dma_pkt_router.xo`）是各自独立编译出来的「零件」。它们在片上怎么连起来——哪个 AIE 端口接到哪个 PL 端口、PL 内核要实例化几份、PL 的 DDR 写口接到哪一块存储——这些「接线说明」必须有人告诉链接器。这份说明就是 `design/system_cfgs/system.cfg`，它是 Versal 系统编译流程里 `[connectivity]`（连接性）配置的一种纯文本形式。

它和传统的 `v++ -l` 命令行参数是等价的两种写法：你可以把每条连接写成命令行参数，也可以集中写进一个 `.cfg` 文件用 `--config` 传进去。本项目选了后者，因为这些连接的数量随 `AIE_SWITCHES` 线性增长，手写易错，于是让 Makefile 生成。

system.cfg 里会出现三类关键指令：

| 指令 | 含义 | 本项目的用途 |
|------|------|--------------|
| `nk=<kernel>:<数量>:<实例1,实例2,...>` | **N**umber of **K**ernels：声明某 PL 内核要实例化几份、各自叫什么名字 | 实例化 `AIE_SWITCHES` 份 `dma_pkt_router` |
| `stream_connect=<源>:<目的>` | 用 AXI4-Stream 把两个流端口连起来 | 把每个 AIE 的 PLIO 输出连到对应 PL 实例的 `pl_stream_in` |
| `sp=<端口>:<存储资源>` | **S**ystem **P**ort：把某个 AXI 端口绑定到系统存储资源（如 DDR） | 把每个 PL 实例的 `ddr_mem` 绑到 DDR |

#### 4.1.2 核心流程

system.cfg 的生成发生在「编译 PL 内核」这条规则里，属于它的**副作用**。整体流程是：

```
1. 从 design/common.h 用 grep + awk 抠出 AIE_SWITCHES 的数值（默认 7）
2. 覆盖写 system.cfg 文件头：注释 + [clock] 段（312.5MHz）+ [connectivity] 段
3. 用 printf + seq 生成 nk 行：列出 dma_pkt_router_0..6 共 7 个实例名
4. for 循环 i=0..6，逐行写 stream_connect（AIE PLIO → PL 流入口）
5. for 循环 i=0..6，逐行写 sp（PL DDR 写口 → DDR）
6. 然后才真正开始 v++ -c --mode hls 编译 PL 内核，产出 dma_pkt_router.xo
```

注意顺序：**system.cfg 是在编译 PL 内核（`.xo`）的过程中被写出的**，而不是在链接 XSA 时才写。这样当后续链接规则把 `system.cfg` 当成依赖时，它已经存在了。

#### 4.1.3 源码精读

system.cfg 的生成全部嵌在 `dma_pkt_router.xo` 这条规则里。先看规则头与取宏值：

[Makefile:200-201](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L200-L201) —— 规则以 `design/common.h` 等为依赖，进入配方后第一件事就是 `grep` 出 `AIE_SWITCHES`：

```makefile
${PL_BUILD_DIR}/dma_pkt_router.xo: design/pl/dma_pkt_router.cpp design/pl/dma_pkt_router.h design/pl/pkt_router_config.cfg ${PROJECT_DIR}/design/common.h
	@AIE_SWITCHES=$$(grep '^#define AIE_SWITCHES' ${PROJECT_DIR}/design/common.h | awk '{print $$3}'); \
```

> 这里的 `$$` 是 Makefile 里转义出的单个 `$`，交给 shell；`awk '{print $$3}'` 取 `#define AIE_SWITCHES 7` 这行的第三列，即数值 `7`。于是 Makefile 不需要你手动维护实例数，改 `common.h` 一处，system.cfg 自动跟着变。

接下来写文件头与 `[clock]`、`[connectivity]` 段：

[Makefile:202-206](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L202-L206) —— 用一连串 `echo`/`echo -e` 追加（第一行是 `>` 覆盖写）：

```makefile
echo "# THIS FILE IS AUTO-GENERATED FROM THE MAKEFILE" > .../system.cfg; \
echo -e "\n[clock]" >> .../system.cfg; \
echo -e "default_freqhz=312500000\n" >> .../system.cfg; \
echo "[connectivity]" >> .../system.cfg; \
echo -e "\n### DMA PACKET ROUTER CONTROLLER ###" >> .../system.cfg; \
```

> `[clock]` 段里 `default_freqhz=312500000` 即 312.5 MHz。这个数不是随便填的——它必须和 PL 内核综合时使用的时钟一致。对照 [design/pl/pkt_router_config.cfg:2](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/pkt_router_config.cfg#L2) 的 `clock=312.5MHz`，两边严格对齐。system.cfg 在此处相当于「向系统声明：我这个 PL 内核是按 312.5MHz 跑的，请按时钟约束接好。」

然后是 `nk` 行，这是最巧妙的一处 shell 拼接：

[Makefile:207-209](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L207-L209)：

```makefile
routers=$$(printf "dma_pkt_router_%s," $$(seq 0 $$((AIE_SWITCHES - 1)))); \
routers=$${routers%,}; \
echo "nk=dma_pkt_router:$$AIE_SWITCHES:$$routers" >> .../system.cfg; \
```

> 拆开看：`seq 0 6` 产出 `0 1 2 3 4 5 6`；`printf "dma_pkt_router_%s,"` 给每个数套上模板并加逗号，得到 `dma_pkt_router_0,dma_pkt_router_1,…,dma_pkt_router_6,`（末尾多一个逗号）；`${routers%,}` 是 shell 参数扩展，删掉末尾那个逗号。最终写出形如 `nk=dma_pkt_router:7:dma_pkt_router_0,dma_pkt_router_1,…,dma_pkt_router_6` 的一行。冒号三段分别是「内核名 : 实例数 : 实例名列表」。

接着两个 `for` 循环分别写连接和存储端口绑定：

[Makefile:210-213](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L210-L213) —— `stream_connect`，把 AIE 图的每个 PLIO 输出接到对应 PL 实例的流入口：

```makefile
echo -e "\n# Connect AIE graph's plio_pkt_rtr_out_0_# to PL kernel's pl_stream_in" >> .../system.cfg; \
for i in $$(seq 0 $$((AIE_SWITCHES - 1))); do \
    echo "stream_connect=ai_engine_0.plio_pkt_rtr_out_0_$$i:dma_pkt_router_$$i.pl_stream_in" >> .../system.cfg; \
done; \
```

> 这是本讲最关键的一行。左端 `ai_engine_0.plio_pkt_rtr_out_0_$i` 必须和 ADF 图里真实的 PLIO 端口名一字不差。回看 [design/aie/graph.h:71-73](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L71-L73)：端口名是 `"plio_pkt_rtr_out_" + bp_graph_insts + "_" + bp_subgraph_insts`，顶层图只有一张所以 `bp_graph_insts=0`，子图序号 `bp_subgraph_insts` 从 0 递增——于是端口名正是 `plio_pkt_rtr_out_0_0`、`plio_pkt_rtr_out_0_1`、…。Makefile 的循环下标 `$i` 与 ADF 的子图计数器是同一个含义，两端天然对齐。右端 `dma_pkt_router_$i.pl_stream_in` 则指向 [design/pl/dma_pkt_router.cpp:11-13](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L11-L13) 里 `#pragma HLS INTERFACE axis port=pl_stream_in` 声明的流入口。`<实例名>.<端口名>` 的点号写法用于在多实例里精确定位某一个实例的某一个端口。

[Makefile:214-217](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L214-L217) —— `sp`，把每个 PL 实例的 DDR 主写口绑到 DDR：

```makefile
echo -e "\n# System port connection linking dma_pkt_router_0 instance to mem resource" >> .../system.cfg; \
for i in $$(seq 0 $$((AIE_SWITCHES - 1))); do \
    echo "sp=dma_pkt_router_$$i.ddr_mem:DDR" >> .../system.cfg; \
done; \
```

> `ddr_mem` 对应 [design/pl/dma_pkt_router.cpp:12-14](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/dma_pkt_router.cpp#L12-L14) 的 `m_axi port=ddr_mem ... bundle=gmem`。`sp=...:DDR` 告诉系统：这个 `m_axi` 口要去访问 DDR。回想 u6-l1，每个 PL 实例按 `ddr_offset = instance_id * SAMPLES_PER_KERN` 写到不重叠的区段，7 个实例共同把整幅图像拼进同一块 DDR——前提就是这里把它们都绑到了同一个 `DDR` 资源上。

写完 system.cfg 之后，规则才进入真正的 HLS 编译：

[Makefile:220-223](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L220-L223)：

```makefile
v++ -c --mode hls --platform=${PLATFORM} -t ${PL_TARGET} \
    --work_dir=${PROJECT_DIR}/${BUILD_DIR}/Work \
    --config ${PROJECT_DIR}/design/pl/pkt_router_config.cfg; \
mv ${PROJECT_DIR}/${BUILD_DIR}/Work/dma_pkt_router.xo ./
```

> 这里 `--config` 吃的是 PL 内核自己的 [pkt_router_config.cfg](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/pl/pkt_router_config.cfg)（指明顶层函数、输出 xo 格式、testbench），与刚生成的 `system.cfg` 是两份不同的配置文件，不要混淆：前者管「怎么综合这个内核」，后者管「整个系统里这个内核怎么和别人连线」。

把以上片段串起来，`AIE_SWITCHES=7`（默认）时生成的 `system.cfg` 长这样（示意，省略部分重复行）：

```ini
# THIS FILE IS AUTO-GENERATED FROM THE MAKEFILE

[clock]
default_freqhz=312500000

[connectivity]

### DMA PACKET ROUTER CONTROLLER ###
nk=dma_pkt_router:7:dma_pkt_router_0,dma_pkt_router_1,dma_pkt_router_2,dma_pkt_router_3,dma_pkt_router_4,dma_pkt_router_5,dma_pkt_router_6

# Connect AIE graph's plio_pkt_rtr_out_0_# to PL kernel's pl_stream_in
stream_connect=ai_engine_0.plio_pkt_rtr_out_0_0:dma_pkt_router_0.pl_stream_in
stream_connect=ai_engine_0.plio_pkt_rtr_out_0_1:dma_pkt_router_1.pl_stream_in
...（共 7 行）

# System port connection linking dma_pkt_router_0 instance to mem resource
sp=dma_pkt_router_0.ddr_mem:DDR
sp=dma_pkt_router_1.ddr_mem:DDR
...（共 7 行）
```

#### 4.1.4 代码实践

**实践目标**：亲手推导 `AIE_SWITCHES=2` 时 system.cfg 应该包含哪些 `nk`、`stream_connect`、`sp` 行，验证你能脱离 Makefile 独立写出这份连接描述。

**操作步骤**：

1. 假装把 `design/common.h` 里的 `#define AIE_SWITCHES 7` 改成 `2`（不要真改源码，只在脑子里推演）。
2. 模仿 [Makefile:207-217](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L207-L217) 的 shell 逻辑，把 `seq 0 1` 代进去，逐行写出 `nk`、两条 `stream_connect`、两条 `sp`。
3. 写完后，对照「预期结果」自检。

**预期结果**（`AIE_SWITCHES=2`）：

```ini
# THIS FILE IS AUTO-GENERATED FROM THE MAKEFILE

[clock]
default_freqhz=312500000

[connectivity]

### DMA PACKET ROUTER CONTROLLER ###
nk=dma_pkt_router:2:dma_pkt_router_0,dma_pkt_router_1

# Connect AIE graph's plio_pkt_rtr_out_0_# to PL kernel's pl_stream_in
stream_connect=ai_engine_0.plio_pkt_rtr_out_0_0:dma_pkt_router_0.pl_stream_in
stream_connect=ai_engine_0.plio_pkt_rtr_out_0_1:dma_pkt_router_1.pl_stream_in

# System port connection linking dma_pkt_router_0 instance to mem resource
sp=dma_pkt_router_0.ddr_mem:DDR
sp=dma_pkt_router_1.ddr_mem:DDR
```

> 自检要点：`nk` 行实例名末尾**不能有多余逗号**；`stream_connect` 左端的 PLIO 名第二个下标必须从 `_0_0` 开始且与右端 `dma_pkt_router_` 的下标严格一一对应。

**待本地验证**：若有 Vitis 环境，可真的把 `common.h` 改成 `AIE_SWITCHES 2`（注意此时还要保证 u1-l4 的整除约束 `(RC_SAMPLES*PULSES)/IMG_SOLVERS` 仍成立，否则会损坏图像），跑 `make pl`，再 `cat design/system_cfgs/system.cfg` 与你手写的版本逐行比对。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `stream_connect` 的左端写成 `plio_pkt_rtr_out_1_0`（第一个下标改成 1），会发生什么？

> **答案**：链接器找不到名为 `plio_pkt_rtr_out_1_0` 的端口——因为顶层图只有一张（`bp_graph_insts=0`），ADF 侧根本不存在 `_1_` 开头的 PLIO。链接会报未解析端口错误而失败。这正说明 Makefile 的 `_0_$i` 写法不是随意，而是与 ADF 图的实例计数器严格耦合。

**练习 2**：`nk` 行里实例名列表能不能省略，只写 `nk=dma_pkt_router:7`？

> **答案**：列表的作用是给每个实例一个**具名**身份（`dma_pkt_router_0`、…、`dma_pkt_router_6`），这样后面的 `stream_connect` 和 `sp` 才能用 `dma_pkt_router_$i.xxx` 精确指代某一个实例的端口。如果省略，链接器会自动生成默认名，`stream_connect`/`sp` 里就无从引用，本项目必须显式列出。

---

### 4.2 用 v++ -g -l 链接 AIE 与 PL 生成 XSA

#### 4.2.1 概念说明

`libadf.a`（AIE 图）和 `dma_pkt_router.xo`（PL 内核）各自是独立的「对象文件」。要把它们焊成一片能跑在 Versal 上的完整硬件设计，需要一次**链接（link）**。在 Vitis 的 Versal AI Engine 流程里，这一步由 `v++ -g -l` 完成：

- `-l`（link）：链接模式。
- `-g`（generate）：在链接的同时生成输出产物（嵌入式处理器设计，即 Vivado 工程），最终产出一份 **XSA**（Xilinx Support Archive，硬件交接文件）。

类比传统编译器：`v++ -c` 像 `cc -c`（编译出 `.o`），`v++ -l` 像 `ld`（链接），而 XSA 就是这次链接的「可执行硬件」。XSA 里封装了 AIE 阵列的配置、PL 内核的网表、二者在 NoC/AXI4-Stream 上的连接关系，以及 ARM PS（处理系统）的地址映射。

#### 4.2.2 核心流程

```
输入：
  - dma_pkt_router.xo     （PL 对象文件）
  - libadf.a              （AIE 图库）
  - system.cfg            （接线说明：nk/stream_connect/sp/clock）
链接命令：v++ -g -l --platform <平台> -t <TARGET> --config system.cfg -o <XSA> libadf.a dma_pkt_router.xo
输出：
  - sar_backproject_<TARGET>.xsa   （完整硬件设计）
```

关键点：system.cfg 在这一步才真正被「消费」——链接器读它的 `[connectivity]` 段，按 `stream_connect` 把 AIE 的 PLIO 与 PL 的 `pl_stream_in` 接通，按 `sp` 把 `ddr_mem` 绑到 DDR，按 `nk` 实例化出 7 份 `dma_pkt_router`。所以 XSA 规则把 `system.cfg` 列为依赖。

#### 4.2.3 源码精读

XSA 规则的头一行就把三个依赖交代清楚了：

[Makefile:257](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L257)：

```makefile
${XSA_BUILD_DIR}/${XSA}: ${PL_BUILD_DIR}/dma_pkt_router.xo ${AIE_BUILD_DIR}/libadf.a design/system_cfgs/system.cfg
```

> 三个依赖缺一不可：`.xo`（PL 零件）、`libadf.a`（AIE 零件）、`system.cfg`（接线说明）。其中 `system.cfg` 由上一节 4.1 的 `.xo` 规则作为副作用生成，所以 Make 的依赖图能保证链接前它已存在。

链接命令本身：

[Makefile:258-266](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L258-L266)：

```makefile
mkdir -p ${XSA_BUILD_DIR}; \
cd ${XSA_BUILD_DIR}; \
v++ -g -l --platform ${PLATFORM} -t ${TARGET} \
    --save-temps \
    --verbose \
    --config ${PROJECT_DIR}/design/system_cfgs/system.cfg \
    -o ${XSA} \
    ${PROJECT_DIR}/${AIE_BUILD_DIR}/libadf.a \
    ${PROJECT_DIR}/${PL_BUILD_DIR}/dma_pkt_router.xo
```

> 逐项看：`-g -l` 即「链接并生成」；`--platform ${PLATFORM}` 指定目标平台（VCK190 基础平台，见 u1-l3 与 env_setup.sh）；`-t ${TARGET}` 让 XSA 区分 hw / hw_emu / sw_emu（产物名 `sar_backproject_${TARGET}.xsa` 也因此分目录隔离）；`--save-temps` / `--verbose` 保留中间产物便于排错（`metrics` 目标还会用到其中的 Vivado 工程）；`--config` 喂入 system.cfg；`-o` 指定输出 XSA；最后两个位置参数是被链接的 `libadf.a` 与 `.xo`。

注意 `metrics` 目标复用的就是这个链接产物里留下的 Vivado 工程：

[Makefile:187-191](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L187-L191) 里 `vivado ... prj.xpr` 的路径正来自 `--save-temps` 保存下来的链接临时目录。所以 XSA 链接不只是「产出一个文件」，它的中间工程还是后续资源/功耗度量的输入（详见 u8-l2）。

#### 4.2.4 代码实践

**实践目标**：理清 XSA 链接的输入输出，并解释为什么 `system.cfg` 必须是它的依赖。

**操作步骤**：

1. 在 [Makefile:257-266](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L257-L266) 处画一张「输入 → 命令 → 输出」的小图，标注每个输入文件的来源规则（哪个目标产出它）。
2. 回答：如果把 `design/system_cfgs/system.cfg` 从依赖列表里删掉，链接步骤还能跑吗？为什么？

**预期结果**：

- 输入：`dma_pkt_router.xo`（来自 [Makefile:200](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L200) 规则）、`libadf.a`（来自 [Makefile:230](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L230) 规则）、`system.cfg`（由 `.xo` 规则副作用生成）。
- 输出：`build/${TARGET}/xsa/sar_backproject_${TARGET}.xsa`。
- 删依赖的后果：因为 `system.cfg` 是 `.xo` 规则的副作用而非独立目标，删掉这条依赖后 Make 不会保证它在链接前被重新生成（例如 `make -j` 并行时，或 `.xo` 已存在而 `common.h` 变了），可能拿到的 `system.cfg` 与当前 `AIE_SWITCHES` 不一致，导致链接出的硬件拓扑与 AIE 图对不上。**显式列为依赖是为了正确表达这条数据依赖**，让 Make 的增量构建可靠。

**待本地验证**：跑 `make` 后查看 `build/hw/xsa/` 下是否生成 `sar_backproject_hw.xsa`，并确认 `--save-temps` 留下的 `_x/link/vivado/vpl/prj/prj.xpr` 存在（它是 `metrics` 目标的输入）。

#### 4.2.5 小练习与答案

**练习 1**：`v++ -g -l` 里的 `-g` 去掉会怎样？

> **答案**：`-l` 只链接不生成输出产物。在 Versal AIE 流程里，省略 `-g` 会得不到完整的嵌入式处理器设计（XSA）。`-g` 是让链接器把网表落地成可使用的工程文件所必需的。

**练习 2**：为什么 XSA 产物名要带 `${TARGET}` 后缀（`sar_backproject_hw.xsa` vs `sar_backproject_sw_emu.xsa`）？

> **答案**：hw / hw_emu / sw_emu 三种目标对应的硬件实现完全不同（真实综合 vs 仿真模型 vs x86 软件模型），且 [Makefile:36](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L36) 用 `BUILD_DIR = build/${TARGET}` 把所有产物按 TARGET 分目录隔离。带后缀既避免互相覆盖，也让人一眼看出这份 XSA 是给哪种运行环境用的。

---

### 4.3 用 v++ -p 打包 SD 卡镜像与 --package 选项

#### 4.3.1 概念说明

链接得到的 XSA 只是「硬件」。一块能启动的 Versal 板卡还需要：引导加载程序（bootloader）、Linux 内核、设备树（DTB）、根文件系统（rootfs），以及我们自己的主机可执行文件（`sar_backproject.elf`）和输入数据。**打包（package）** 这一步（`v++ -p`）就是把硬件 XSA 和上述所有软件/数据组装成一张可写入 SD 卡的启动镜像（含 `BOOT.BIN`、`image.ub`、rootfs 等）。

这一步由 [Makefile:91](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L91) 的 `package` 目标驱动。注意 `package` 是 Makefile 里的**第一个实际规则目标**，所以裸 `make`（README 推荐的入口）就等于 `make package`——它会先把 `.xo`、`libadf.a`、host elf、XSA 全部造齐，最后打包。

#### 4.3.2 核心流程

```
package 依赖：dma_pkt_router.xo, libadf.a, sar_backproject.elf, sar_backproject_${TARGET}.xsa
         │
         ├── grep 出 RC_SAMPLES，决定要打包哪份 phdata CSV
         ├── 据 TARGET 决定是否带 DTB（仅 hw 带）
         └── v++ -p 把下面所有东西打包成 SD 卡镜像：
                 ├── 硬件：XSA + libadf.a
                 ├── 启动链：BL31_ELF, UBOOT, IMAGE(内核), ROOTFS, [DTB]
                 ├── 启动方式：boot_mode=sd, image_format=ext4
                 ├── AIE 延迟启动：defer_aie_run
                 └── 额外 SD 文件：run_script, xrt.ini, elf, slowtime CSV, phdata CSV
```

#### 4.3.3 源码精读

先看一个与 TARGET 相关的条件，它体现了 hw 与仿真模式在打包上的差异：

[Makefile:86-90](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L86-L90)：

```makefile
ifeq ($(TARGET),hw)
    DTB_OPTION := --package.dtb ${DTB}
else
    DTB_OPTION :=
endif
```

> 注释解释得很直白：自定义 DTB 在 QEMU（仿真）下会让内核 panic 崩溃，所以**只在 hw 打包时带 DTB**，仿真模式留空。这是「为不同运行环境差异化打包」的一个实例。

`package` 目标的依赖与开头：

[Makefile:91-94](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L91-L94)：

```makefile
package: ${PL_BUILD_DIR}/dma_pkt_router.xo ${AIE_BUILD_DIR}/libadf.a ${HOST_BUILD_DIR}/${HOST_EXE} ${XSA_BUILD_DIR}/${XSA} 
	@RC_SAMPLES=$$(grep '^#define RC_SAMPLES' ${PROJECT_DIR}/design/common.h | awk '{print $$3}'); \
	mkdir -p ${PACKAGE_BUILD_DIR}; \
	cd ${PACKAGE_BUILD_DIR}; \
```

> 依赖列了四样：PL `.xo`、AIE `libadf.a`、host `elf`、XSA。和 4.2 的 XSA 规则对照看，这是一条自上而下的依赖链：`package → XSA → (.xo, libadf.a, system.cfg)`，而 `.xo` 规则又顺带生成 system.cfg，host elf 又依赖 libadf.a（见 u1-l3）。于是 `make package` 一条命令足以触发全链路构建。注意这里又 `grep` 了一次 `RC_SAMPLES`，目的是挑出与当前配置匹配的那份 phdata CSV（文件名里含样本数，如 `gotcha_phdata_512-out-of-424-...csv`），保证打包进去的数据与 AIE 编译用的列数一致。

核心的 `v++ -p` 命令：

[Makefile:95-109](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L95-L109)：

```makefile
v++ -p -t ${TARGET} -f ${PLATFORM} \
    $(DTB_OPTION) \
    --package.bl31_elf ${BL31_ELF} \
    --package.uboot ${UBOOT} \
    --package.kernel_image ${IMAGE} \
    --package.rootfs ${ROOTFS} \
    --package.boot_mode=sd \
    --package.image_format=ext4 \
    --package.defer_aie_run \
    --package.sd_file ${PROJECT_DIR}/design/exec_scripts/run_script_${TARGET}.sh \
    --package.sd_file ${PROJECT_DIR}/design/profiling_cfgs/xrt.ini \
    --package.sd_file ${PROJECT_DIR}/${HOST_BUILD_DIR}/${HOST_EXE} \
    --package.sd_file ${PROJECT_DIR}/design/test_data/gotcha_slowtime_pass1_360deg_HH.csv \
    --package.sd_file ${PROJECT_DIR}/design/test_data/gotcha_phdata_$${RC_SAMPLES}-out-of-424-rc-samples_pass1_360deg_HH.csv \
    ${PROJECT_DIR}/${XSA_BUILD_DIR}/${XSA} ${PROJECT_DIR}/${AIE_BUILD_DIR}/libadf.a
```

逐类拆解这些 `--package` 选项（它们的值大多来自 [helper_scripts/env_setup.sh](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/env_setup.sh) 里导出的 Yocto 构建产物路径）：

| 选项 | 含义 | 本项目取值来源 |
|------|------|----------------|
| `--package.bl31_elf` | ARM Trusted Firmware（BL31），安全监控态固件 | env_setup.sh 的 `BL31_ELF` |
| `--package.uboot` | U-Boot 引导加载器 | env_setup.sh 的 `UBOOT` |
| `--package.kernel_image` | Linux 内核镜像（`Image`） | env_setup.sh 的 `IMAGE` |
| `--package.rootfs` | 根文件系统（tar.gz） | env_setup.sh 的 `ROOTFS` |
| `--package.dtb` | 设备树（仅 hw） | env_setup.sh 的 `DTB`，经 `$(DTB_OPTION)` 注入 |
| `--package.boot_mode=sd` | 从 SD 卡启动 | 固定 |
| `--package.image_format=ext4` | 镜像分区用 ext4 | 固定 |
| `--package.defer_aie_run` | AIE 图**不在启动时自动运行**，等主机程序显式加载 | 关键：见下文 |
| `--package.sd_file <path>` | 额外拷到 SD 卡的文件（可多次出现） | 见下表 |

`--package.sd_file` 一共塞了 5 类文件到 SD 卡：

| sd_file | 作用 |
|---------|------|
| `run_script_${TARGET}.sh` | 板上 ARM 启动后执行的反投影运行脚本（u3-l1 讲过它如何拼命令行参数） |
| `xrt.ini` | XRT 运行时配置（影响 trace/性能采集，详见 u8-l2） |
| `sar_backproject.elf` | 主机可执行程序本身 |
| `gotcha_slowtime_..._HH.csv` | 输入：slowtime 天线几何数据 |
| `gotcha_phdata_${RC_SAMPLES}-...csv` | 输入：距离压缩回波（文件名按 `RC_SAMPLES` 动态选择） |

最后两个位置参数是**硬件输入**：XSA 与 `libadf.a`。

命令末尾两个位置参数：

> `${XSA}` 提供硬件设计，`libadf.a` 提供 AIE 图的运行时控制元数据（XRT 靠它知道怎么加载/控制 AIE 图，并生成 host 用的 xclbin）。二者合起来才是一份「可被主机驱动的完整加速器」。

**重点理解 `--package.defer_aie_run`**：默认情况下，Vitis 打包的系统会在启动时自动加载并运行 AIE 图；但本项目加了 `defer_aie_run`，让 AIE 图**延迟到主机程序显式 `load_xclbin` + `graph.run(...)` 时才运行**。这正好对应 u3-l1/u3-l5 讲的主机控制流：主机先初始化（`SARBackproject` 构造函数打开 device、加载 xclbin、建 graph 句柄），再 `runGraphs()` 发令、`bp()` 喂数据。换句话说，`defer_aie_run` 把 AIE 的「发车时刻」交还给了主机程序，而不是启动脚本。

#### 4.3.4 代码实践

**实践目标**：把 `v++ -p` 的所有 `--package` 选项分类，搞清楚每样东西最终落到了 SD 卡的哪个角色上，并解释 `defer_aie_run` 与主机程序的关系。

**操作步骤**：

1. 对照 [Makefile:95-109](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L95-L109)，把所有 `--package` 选项分成四组：(a) 启动链；(b) 启动方式/格式；(c) AIE 运行时机；(d) 额外 SD 文件。
2. 对 [helper_scripts/env_setup.sh](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/helper_scripts/env_setup.sh) 里 `BL31_ELF / UBOOT / IMAGE / ROOTFS / DTB` 五个变量，标注它们各自指向 Yocto 构建的哪件产物。
3. 回答：为什么 `defer_aie_run` 是本项目**必须**的？如果不加会怎样？

**预期结果**：

- (a) 启动链：`bl31_elf`、`uboot`、`kernel_image`、`rootfs`、（`dtb` 仅 hw）。
- (b) 启动方式/格式：`boot_mode=sd`、`image_format=ext4`。
- (c) AIE 运行时机：`defer_aie_run`。
- (d) 额外 SD 文件：`run_script_${TARGET}.sh`、`xrt.ini`、`sar_backproject.elf`、slowtime CSV、phdata CSV。
- env 变量对应：`BL31_ELF`→`arm-trusted-firmware.elf`、`UBOOT`→`u-boot.elf`、`IMAGE`→`Image`（Linux 内核）、`ROOTFS`→`jpl-versal-image-...rootfs.tar.gz`、`DTB`→`system.dtb`（见 env_setup.sh）。
- `defer_aie_run` 必要性：本项目的主机程序需要在 AIE 运行**之前**完成一系列准备——构造 `SARBackproject`、`load_xclbin`、分配 buffer、建立 graph/PL kernel 句柄（u3-l2），再按脉冲逐条用 GMIO 投递数据、用 RTP 控制 dump 时机（u3-l5）。若 AIE 在启动时自动运行，那时主机还没喂数据、还没建好句柄，图会因为输入未就绪而行为未定义。延迟到主机显式发令，才能保证「先就绪、再运行」的顺序。

**待本地验证**：无硬件时无法实际打包，但可以确认环境变量——`source helper_scripts/env_setup.sh` 后 `echo $BL31_ELF $UBOOT $IMAGE $ROOTFS $DTB` 应指向 versal-yocto-build 的 deploy 目录（见 README「Yocto Build」与「SAR Application Design Build」两节）。

#### 4.3.5 小练习与答案

**练习 1**：为什么仿真模式（hw_emu / sw_emu）不带 DTB？

> **答案**：见 [Makefile:84-85](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L84-L85) 的注释——自定义 DTB 在 QEMU 仿真下会导致内核 panic 崩溃，所以仅在真实硬件（hw）打包时才注入 DTB，仿真用平台默认的设备树。

**练习 2**：`--package.sd_file` 为什么要把 slowtime / phdata CSV 也塞进 SD 卡？

> **答案**：本项目的输入数据走的是文件方式——主机 `fetchRadarData()`（u3-l3）从这两个 CSV 读雷达数据。把 CSV 一起打包，板卡上电后 `run_script_hw.sh` 启动 `sar_backproject.elf` 时就能直接找到输入文件，无需额外拷贝。文件名里的 `${RC_SAMPLES}` 是 Makefile 现抠的，保证打包的数据列数与 AIE 编译时一致。

**练习 3**：`package` 目标的依赖里为什么**没有** `system.cfg`，而 XSA 规则里有？

> **答案**：`package` 依赖的是 XSA（成品），system.cfg 是 XSA 的内部细节，已被 XSA 规则 [Makefile:257](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L257) 直接依赖。Make 的依赖是「按需传递」的：`package → XSA → system.cfg`，只要 XSA 是最新的（其依赖都满足），`package` 就不必再关心 system.cfg。这体现了依赖图分层——每条规则只关心自己的直接输入。

---

### 4.4 为什么 system_cfgs/ 里只有一个 .gitkeep

最后回答规格里要求解释的一点。`design/system_cfgs/` 目录在仓库里只追踪了一个文件 [design/system_cfgs/.gitkeep](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/system_cfgs/.gitkeep)——一个**空文件**，内容为空。

原因有两层：

1. **Git 不追踪空目录。** Git 的最小单位是文件，一个没有任何文件的目录在 `git add` 时会被直接忽略。但本项目的构建流程需要一个现成的 `design/system_cfgs/` 目录来承接 Makefile 生成的 `system.cfg`（见 [Makefile:202](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L202) 用 `>` 覆盖写到这个路径）。放一个 `.gitkeep` 占位，既让目录被纳入版本控制，又不开一个新约定。

2. **system.cfg 本身是构建产物，被 `.gitignore` 忽略。** 仓库根 `.gitignore` 里有一行 `design/system_cfgs/system.cfg`，显式忽略它。因为它的内容完全由 `AIE_SWITCHES`（与 PL 内核编译）派生，可以被随时重新生成，放进版本控制反而会和源码不同步、制造无意义的 diff。于是「保留目录 + 忽略产物」的组合，就用 `.gitkeep` 来实现。

> 小知识：`.gitkeep` 不是 Git 的官方特性，只是社区约定俗成的「占位文件」名（Git 对文件名没有任何要求）。也有人用 `.gitattributes` 等其它名字，作用一样。

## 5. 综合实践

把本讲的三块知识串起来，完成下面这个端到端的「阅读型实践」。

**任务**：以 `make`（即 `make package`）为起点，画出从「源码」到「SD 卡镜像」的完整依赖与数据流图，并在图上标注每一步用的 `v++` 模式、关键 `--package`/`--config` 选项，以及 system.cfg 在哪一步被生成、在哪一步被消费。

**建议步骤**：

1. 从 [Makefile:91](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L91) 的 `package` 目标出发，沿着依赖箭头向下展开，列出四条间接规则：`.xo`（[Makefile:200](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L200)）、`libadf.a`（[Makefile:230](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L230)）、host elf（[Makefile:247](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L247)）、XSA（[Makefile:257](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/Makefile#L257)）。
2. 在图上用三种颜色/标记区分三个阶段：**编译（`v++ -c`）**、**链接（`v++ -g -l`）**、**打包（`v++ -p`）**。
3. 标出 system.cfg 的生命周期：在 `.xo` 规则里被生成（副作用）→ 在 XSA 规则里被消费（`--config`）→ 被 `.gitignore` 忽略。
4. 在打包阶段，把所有 `--package` 选项按 4.3.3 的表格分类标到图上。
5. 写一段话回答：如果只改 `design/common.h` 里的 `AIE_SWITCHES`，图里哪些节点会失效重建？哪些 `--package.sd_file` 会受影响？（提示：`AIE_SWITCHES` 影响 system.cfg 与 `.xo`、`libadf.a`、XSA、host elf 全链路；`--package.sd_file` 里 phdata CSV 的文件名取决于 `RC_SAMPLES` 而非 `AIE_SWITCHES`，故 sd_file 列表本身不变，但被拷的 elf/xsa 内容变了。）

**预期结果**：一张能解释「为什么改一个宏会触发几乎全量重建」的依赖图，以及一句结论——**system.cfg 是连接 AIE 与 PL 的总线声明，它随 `AIE_SWITCHES` 自动伸缩，是整个集成流程的枢纽**。

## 6. 本讲小结

- `design/system_cfgs/system.cfg` 由 Makefile 在编译 PL 内核（`.xo`）时**自动生成**，内容随 `design/common.h` 里的 `AIE_SWITCHES` 伸缩；它包含三类指令：`nk`（实例化几份 PL 内核）、`stream_connect`（AIE PLIO → PL 流入口）、`sp`（PL DDR 写口 → DDR）。
- `stream_connect` 左端的 PLIO 名 `plio_pkt_rtr_out_0_$i` 必须与 ADF 图 [design/aie/graph.h:71-73](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/graph.h#L71-L73) 的端口命名严格对齐——两端共享同一个从 0 开始的 switch 计数器。
- `v++ -g -l` 用 `--config system.cfg` 把 `libadf.a` 与 `dma_pkt_router.xo` 链接成 XSA（完整硬件设计）；system.cfg 在这一步被真正消费。
- `v++ -p` 把 XSA、`libadf.a`、host elf 与启动链（BL31/u-boot/内核/rootfs/DTB）、输入数据等打包成 SD 卡镜像；`--package.defer_aie_run` 把 AIE 图的运行时机交给主机程序显式控制。
- `design/system_cfgs/` 只追踪一个空 `.gitkeep`，因为 system.cfg 是构建产物，被 `.gitignore` 忽略——放占位文件既保留目录、又不污染版本控制。
- 整条流水线与传统编译器同构：**`v++ -c`（编译零件）→ `v++ -g -l`（链接成硬件 XSA）→ `v++ -p`（打包成可启动镜像）**。

## 7. 下一步学习建议

- 下一篇 **u7-l2「三个分支：main / host_stride / pl_stride」** 会对比三条分支在「输入侧数据预排序」上的取舍，其中 `pl_stride` 分支正因为 system.cfg 无法表达数组化的 AXI4-Stream 端口，目前被限制在 `AIE_SWITCHES=1`——这是对 system.cfg 表达能力的直接约束，读完会有更深的体会。
- 若想从硬件部署角度继续，可接着读 **u7-l3「硬件部署：Yocto、NFS/TFTP 与 JTAG 烧写」**，它讲解 `package` 产物（BOOT.BIN / SD 卡镜像）如何真正落到板卡上。
- 想验证集成正确性的读者，可回看 **u8-l1（仿真流程）** 与 **u8-l2（性能/功耗度量）**：前者用 `aiesim`/`plsim_router` 在不打包的情况下验证 AIE↔PL 连接，后者复用 XSA 链接阶段 `--save-temps` 留下的 Vivado 工程做资源/功耗报告。
