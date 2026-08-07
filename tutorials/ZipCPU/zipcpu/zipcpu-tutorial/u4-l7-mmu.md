# MMU 内存管理单元

## 1. 本讲目标

ZipCPU 的 MMU 是一个**实验性、当前标注为 DEPRECATED** 的内存管理单元。它用 Verilog 写成,试图在「不引入完整操作系统」的前提下,给 supervisor/user 双模式 CPU 加上虚拟地址到物理地址的翻译能力。本讲学完后,你应该能够:

1. 说清楚一个 MMU 在 CPU 与总线之间扮演的「夹心层」角色,以及它为什么是可选的。
2. 读懂 `zipmmu.v` 中一次虚拟地址到物理地址的翻译流程(三拍查表)。
3. 理解页表项(TLB 表项)的字段结构、控制寄存器与状态寄存器的含义。
4. 看懂缺页、写只读页、执行非可执行页这几类错误是如何被检测并上报的。
5. 结合 README 与源码注释,解释为什么这个 MMU 目前没有被真正集成进新 ZipCore,以及作者打算把它放在哪里。

> ⚠️ 重要提醒:本模块**没有**集成进当前默认的 ZipCPU 构建流程。它只在 `bench/formal` 下有形式化证明,在 `zipsystem.v` 中有一段被 `OPT_MMU` 宏保护、但作者明说「未测试」的实例化代码。把它当作「学习地址翻译思想 + 阅读一份真实但停摆的硬件设计」的材料,而不是当前可用的功能。

---

## 2. 前置知识

阅读本讲前,请确保你已经掌握:

- **虚拟内存与物理内存的区别**:程序看到的地址(虚拟地址)和真实连到 RAM 的地址(物理地址)可以不同,中间需要一个翻译机构。
- **页(page)**:把地址空间切成固定大小的块,翻译以「页」为单位进行,页内偏移不变。翻译时只需替换「页号」,页内低位偏移直接拼接。
- **TLB(Translation Lookaside Buffer,旁路翻译缓冲)**:把最近用过的「虚拟页号 → 物理页号」映射存在一个小而快的表里,避免每次访存都去查内存里的页表。`zipmmu.v` 的 TLB 就是用一组寄存器数组实现的。
- **上下文(context)**:不同任务(进程)有自己的页表。MMU 用一个 context 编号区分「当前是哪个任务的地址空间」。
- **supervisor/user 双模式**:这是 u2-l1、u2-l5 讲过的核心概念。MMU 的翻译**只在 user 模式(GIE=1)下生效**,supervisor 模式直接走物理地址。如果你忘了 GIE 是什么,请先复习 u2-l1。
- **Wishbone 总线主/从端口**:u4-l1、u4-l2 讲过的 `cyc/stb/we/addr/data/ack/stall/err` 信号。MMU 一侧是「从端口」(被 CPU 配置),另一侧是「主端口」(向下游内存发翻译后的地址)。

如果你对「页表为什么用虚地址高位当索引」这类问题还不熟,可以先把 MMU 想象成一个「函数 f(虚地址, 上下文) → 物理地址」,本讲要讲的就是这个函数在硬件里怎么用查表 + 缓存实现。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rtl/peripherals/zipmmu.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v) | 本讲的主角。一个「bump-in-the-line」式 Wishbone MMU,顶部的长注释几乎是半篇设计文档,翻译逻辑全部在此文件内。 |
| [README.md](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md) | 「Not yet integrated」一节解释 MMU 的集成现状与作者规划。 |
| [rtl/peripherals/README.md](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/README.md) | 外设清单,称 zipmmu 为「experimental MMU,仅离线测试过」。 |
| [rtl/zipsystem.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v) | 有一段被 `OPT_MMU` 宏保护的 zipmmu 实例化代码,是「计划中的集成点」。 |
| [bench/formal/zipmmu.sby](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/zipmmu.sby) | 用 SymbiYosys 对 zipmmu 做形式化证明的配置文件。 |

---

## 4. 核心概念与源码讲解

### 4.1 MMU 的「夹心层」定位与双总线模型

#### 4.1.1 概念说明

`zipmmu` 的作者把它设计成一个 **「bump-in-the-line」(线路中的凸起)MMU**:它夹在 CPU 的访存通路和真正的外部总线之间,像一个透明的中转站。CPU 发出的(虚拟)地址先进入 MMU,MMU 把它翻译成物理地址,再用这个物理地址去驱动下游总线。

它有**两条 Wishbone 接口**,但有一个关键约束:**这两条总线不会同时活跃**:

- **配置总线(slave 侧,`i_wbs_*`)**:CPU 通过它来读写 MMU 内部的控制寄存器、页表项、状态寄存器。这条线**只在 supervisor 模式下使用**,因为只有内核才有资格配置地址映射。
- **数据总线(master 侧,`i_wbm_*` / `o_*`)**:CPU 平时访存走的这条线。MMU 监听上面的(虚拟)地址,翻译后从 `o_*` 发出(物理)地址。

这个设计的妙处在文件顶部的注释里写得很清楚:让 MMU 可选地「夹」进去,完全由搭建 `ZipSystem` 的人决定要不要它。这样资源紧张的 FPGA 设计可以完全不要 MMU(零开销),而需要虚拟内存的设计则把它插进去。

#### 4.1.2 核心流程

MMU 对外表现为一个状态机,但它的核心其实是一条「翻译流水线」:

```
CPU 发访存请求 (i_wbm_addr, i_wbm_cyc, i_wbm_stb, i_gie)
        │
        ▼
是 supervisor 模式吗? (kernel_context = context==0 或 GIE==0)
        │ 是
        ├─────────────────────────────► 直接用原始地址,不翻译 (o_addr = 输入地址)
        │ 否 (user 模式)
        ▼
是「上一次翻译过的同一页」吗? (last_page_valid && 虚页号匹配)
        │ 是
        ├─────────────────────────────► 复用缓存的物理页号,延迟 1 拍
        │ 否
        ▼
查 TLB(查所有表项) → 命中?
        │ 命中且权限通过
        ├─────────────────────────────► 用 tlb_pdata 翻译,延迟 2 拍,并缓存为 last
        │ 未命中 / 权限错
        ▼
拉高 o_rtn_miss / o_rtn_err,在 status_word 记录原因,让 CPU 停下
```

#### 4.1.3 源码精读

模块的端口与设计意图在文件顶部注释中阐明——注意「Both busses will not be active at the same time」这句,它定义了整个模块的并发模型:

[rtl/peripherals/zipmmu.v:7-22](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L7-L22) — 用注释说明这是一个从一条总线接收配置、改写另一条总线地址的 Wishbone MMU,且特意设计成「可选」插入。

端口分两组对应两条总线。配置侧(slave)是一组精简的 `cyc_stb/we/addr/data`:

[rtl/peripherals/zipmmu.v:289-295](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L289-L295) — 配置总线端口,`i_wbs_addr` 只有 `(LGTBL+2)` 位宽,因为只需寻址「控制字 / 状态字 / 若干页表项」这点空间。

数据侧(master)是完整的 Wishbone 主端口,多了一个 `i_gie` 输入用来判断当前是不是 user 模式:

[rtl/peripherals/zipmmu.v:297-313](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L297-L313) — 数据总线端口,`i_gie` 是翻译的总开关。

#### 4.1.4 代码实践

**实践目标**:在脑中建立「两条总线、一个翻译核心」的模型。

**操作步骤**:

1. 打开 `rtl/peripherals/zipmmu.v`,找到模块声明(约第 233 行的 `module zipmmu(...)`)。
2. 用纸笔把端口分成两栏:左栏抄 `i_wbs_*`/`o_wbs_*`,右栏抄 `i_wbm_*`/`o_*`/`i_stall/i_ack/i_err`/`o_rtn_*`。
3. 在每栏顶部标注「配置总线(只在 supervisor 用)」和「数据总线(翻译通路)」。

**需要观察的现象**:两条总线的信号是严格分组的,没有任何一个 `wbs_` 信号直接参与 `o_addr`(下游物理地址)的运算——配置和数据两条路是分开的。

**预期结果**:你会得到一张「左右两栏 + 一个 `i_gie` 输入」的图,这正是「bump-in-the-line」的直观含义。

#### 4.1.5 小练习与答案

**练习 1**:为什么作者要求两条总线「不能同时活跃」?如果它们同时活跃会怎样?

> 参考答案:配置总线和数据总线共享了模块内的 TLB 寄存器数组等资源,且翻译流水线是单条顺序处理的。同时活跃会导致「一边正在改页表、另一边正在按旧页表翻译」的竞态。作者用注释约定两者互斥(形式化证明里也用 `assume((!i_wbm_cyc)||(!i_wbs_cyc_stb))` 强制了这一点),从而省掉了复杂的锁与仲裁电路。

**练习 2**:`i_gie` 信号在这里起什么作用?

> 参考答案:它是「是否需要翻译」的总开关。supervisor 模式(GIE=0)直接走物理地址,不翻译;只有 user 模式(GIE=1)且 context≠0 时才激活翻译。这保证了内核永远能访问真实物理内存,不会被自己的页表挡住。

---

### 4.2 地址翻译机制:TLB 查找与三拍流水

#### 4.2.1 概念说明

这是本讲最核心的部分。MMU 把一次翻译拆成**三个时钟周期**的流水,每个周期做一件事:

- **第 1 拍**:把 CPU 数据总线上的请求(虚地址、读/写、是否取指)锁存进一组 `r_` 寄存器。这一拍同时也判断「是不是 supervisor 模式」「是不是上次翻译过的同一页」。
- **第 2 拍**:拿锁存的虚页号去**并行比较所有 TLB 表项**,得到一组命中向量 `r_tlb_match`,并算出命中表项的下标 `s_tlb_addr`、是否全部未命中 `s_tlb_miss`、是否恰好命中一个 `s_tlb_hit`。
- **第 3 拍**:用 `s_tlb_addr` 读出该表项的物理页号 `ppage` 和权限标志位,拼接成最终的物理地址 `o_addr`,并检查读/写/执行权限。

之所以要三拍,是因为 TLB 是一个「寄存器数组」,而 Verilog 里读数组需要地址已稳定一拍。先比较(第 2 拍)拿到下标,再用下标读(第 3 拍),这是典型的「先译码再读存储」的两级组合逻辑。

作者在注释里给出了延迟目标:命中上次同一页(supervisor 或缓存命中)只损失 **1 拍**;打开一个新页损失 **2 拍**;任何一次访问最多停顿 2 拍,且可流水化。

#### 4.2.2 核心流程

虚地址到物理地址的拼接关系(理解这个,翻译就懂了一半):

```
虚拟地址 (DW-2 位, 因最低 2 位字节偏移在 zipcore 外)
├────────虚页号 VAW 位────────┤──页内偏移 LGPGSZW 位──┤
                │                         │
   经 TLB 查表替换为物理页号        直接照搬(不翻译)
                │                         │
                ▼                         ▼
物理地址 = [ ppage (PAW 位) ] ++ [ 页内偏移 (LGPGSZW 位) ]
```

其中宽度由综合期参数推出:`VAW = DW - LGPGSZB`(虚页号位数),`PAW = AW - LGPGSZW`(物理页号位数),`LGPGSZW = LGPGSZB - 2`(页大小,以字为单位)。

三拍查表的伪代码:

```
# 第 1 拍
r_addr   <= i_wbm_addr            # 锁存请求
r_we     <= i_wbm_we
r_exe    <= i_wbm_exe
r_pending<= 需要查表 && 不是同一页
r_valid  <= supervisor 或 同一页已命中   # 直接放行,不等查表

# 第 2 拍 (并行比较所有 TBL_SIZE 个表项)
for k in 0..TBL_SIZE-1:
    r_tlb_match[k] = tlb_valid[k]
                     && tlb_vdata[k] == r_vpage        # 虚页号匹配
                     && tlb_cdata[k] 上下文匹配         # context 匹配
s_tlb_addr = 命中的那个下标
s_tlb_miss = (全部没命中)
s_tlb_hit  = (恰好命中一个)

# 第 3 拍
s_tlb_flags = tlb_flags[s_tlb_addr]
ppage       = tlb_pdata[s_tlb_addr]
o_addr      = { ppage, r_addr 的页内偏移 }
# 同时检查权限,产生 simple_miss / ro_miss / exe_miss / table_err
```

#### 4.2.3 源码精读

**虚/物理页号与地址的拼接关系**,定义在派生参数和这几行赋值里。注意 `r_vpage` 取的是锁存地址的高位(虚页号),`o_addr` 用 `ppage` 替换高位、保留低位页内偏移:

[rtl/peripherals/zipmmu.v:524-527](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L524-L527) — `r_vpage` 从锁存地址抽取虚页号,`r_ppage` 从已翻译的 `o_addr` 高位抽取物理页号(用于形式化校验)。

第 1 拍的锁存逻辑(`r_pending`/`r_valid`/`o_addr` 的三种情况:same-page / kernel / 需查表):

[rtl/peripherals/zipmmu.v:532-577](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L532-L577) — 这是「Step two — handle the page lookup」的入口。注意 `o_addr` 在 supervisor 模式直接用输入地址高位,在查表命中后改用 `ppage`,页内偏移永远照搬。

第 2 拍的**并行比较所有表项**(这是 TLB 的精髓,用 `generate for` 展开,综合后是一组并行比较器):

[rtl/peripherals/zipmmu.v:590-601](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L590-L601) — 对每个表项,要求「有效位为真 + 虚页号相等 + 上下文匹配」三者同时成立才算命中。

紧接着计算出命中下标和 miss/hit 标志:

[rtl/peripherals/zipmmu.v:603-618](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L603-L618) — `s_tlb_addr` 是命中表项的下标(遍历找最后一个命中的),`s_tlb_miss` 表示一个都没命中,`s_tlb_hit` 表示恰好命中一个。

第 3 拍:用下标读出物理页号、标志位,并产生各种 miss 信号:

[rtl/peripherals/zipmmu.v:622-631](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L622-L631) — 读出 `ppage`(物理页号)和四个标志;`simple_miss`=页没找到,`ro_miss`=写只读页,`exe_miss`=执行非可执行页,`table_err`=命中了多个(表损坏)。

**同一页缓存**(`last_page_valid`)是性能关键:它把「同一页内的连续访问」从 2 拍降到 1 拍:

[rtl/peripherals/zipmmu.v:382-389](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L382-L389) — `this_page_valid` 判断「当前访问仍在上次命中的同一页且权限相符」,成立则跳过查表直接放行。

#### 4.2.4 代码实践

**实践目标**:把一次 user 模式下的「跨页访问」翻译过程,用纸笔走一遍三拍流水。

**操作步骤**:

1. 假设参数取默认值:`LGTBL=6`(64 个 TLB 表项)、`PLGPGSZB=20`(页大小 \(2^{20}\) 字节,即 1 MB,合 \(2^{18}\) 字)。因此 `VAW = 32 - 20 = 12` 位虚页号,`LGPGSZW = 18` 位页内偏移。
2. 设当前 user 任务 context=5,它访问虚地址 `0x00123456`。
   - 虚页号 = 高 12 位 = `0x001`,`r_vpage = 0x001`,页内偏移 = `0x23456`。
3. CPU 用 supervisor 身份预先往 TLB 写了一条表项:`{context=5, vpage=0x001, ppage=0x010, flags=可读可写}`。
4. 走三拍:
   - 第 1 拍:`r_vpage=0x001` 被锁存,`r_pending=1`(假设不是同一页)。
   - 第 2 拍:并行比较 64 个表项,那条 context=5、vpage=0x001 的命中,`s_tlb_hit=1`、`s_tlb_addr` 指向它。
   - 第 3 拍:`ppage=0x010`,权限检查通过,`o_addr = {0x010, 0x23456}`(低位 18 位照搬)。

**需要观察的现象**:最终物理地址的高位(页号)被换成了 `0x010`,而低 18 位(页内偏移)`0x23456` 一字未改。

**预期结果**:物理地址 ≈ `0x01023456`。这就是「翻译只换页号、偏移不变」的直观体现。

> 这个例子中的数值是为了演示翻译拼接关系而构造的示例,不是从真实测试中截取的;`o_addr` 的精确位拼接请以上一节源码为准。

#### 4.2.5 小练习与答案

**练习 1**:为什么第 2 拍要「先比较拿地址」,第 3 拍才「用地址读数组」,而不是一拍内完成?

> 参考答案:因为 `tlb_pdata`/`tlb_flags` 是寄存器数组,读取需要稳定的索引。第 2 拍比较得到命中下标 `s_tlb_addr`,它要等到下一个时钟边沿才稳定可用,所以必须到第 3 拍才能拿它去读 `tlb_pdata[s_tlb_addr]`。强行在一拍内做「比较 + 用结果读数组」会形成组合逻辑环或超长路径,无法在目标时钟下收敛。

**练习 2**:同一次访存,什么时候只需 1 拍,什么时候需要 2 拍?

> 参考答案:当访问落在「上次刚翻译过、且已缓存的同一页」(`this_page_valid`/`last_page_valid` 成立),或处于 supervisor 模式(`kernel_context`)时,直接放行,损失 1 拍。当访问的是一个新页,需要完整走 TLB 查表,损失 2 拍。这正是注释里「one clock for same page / two clocks for a new page」的来源。

---

### 4.3 页表项结构与控制/状态字

#### 4.3.1 概念说明

`zipmmu` 没有像传统 x86/ARM 那样在内存里维护一棵多级页表树,而是把**整张页表都做成片上寄存器数组**(`TBL_SIZE` 个表项,默认 64 个)。这本质上是一个「全相连、全片上」的 TLB——没有「访存查页表」这一步,所以也避免了页缺失时的内存往返,代价是表项数量有限、可扩展性差。

每个 TLB 表项由几组并行数组共同组成:

| 数组 | 内容 |
|------|------|
| `tlb_valid[TBL_SIZE]` | 该表项是否有效(一位) |
| `tlb_vdata[k]` | 虚页号(VAW 位) |
| `tlb_pdata[k]` | 物理页号(PAW 位) |
| `tlb_cdata[k]` | 上下文编号(LGCTXT 位) |
| `tlb_flags[k]` | 4 位权限标志:只读 RO、可执行 EXE、可缓存 CH、已访问 AX |
| `tlb_accessed[k]` | 硬件置位的「已访问」位,可供软件做 LRU |

CPU 通过**配置总线**读写这些表项。地址译码用两位把它们分成四类:控制字、状态字、虚地址半页表项、物理地址半页表项。

#### 4.3.2 核心流程

配置总线的地址译码(`i_wbs_addr` 的最高位和最低位):

```
i_wbs_addr[LGTBL+1] = 0, [0] = 0  →  控制字 (control)      读写当前 context / 读参数
i_wbs_addr[LGTBL+1] = 0, [0] = 1  →  状态字 (status)       只读,记录上次缺页原因
i_wbs_addr[LGTBL+1] = 1, [0] = 0  →  虚页表项半字 (vtable) 写虚页号 + 低位上下文 + 标志
i_wbs_addr[LGTBL+1] = 1, [0] = 1  →  物理页表项半字 (ptable)写物理页号 + 高位上下文,并置 valid
```

注意一个关键约定:**写 vtable 半字时同时写入标志位和低位上下文;写 ptable 半字时才把 `tlb_valid` 置 1**。也就是说,建立一条映射的完整流程是「先写 vtable 半字,再写 ptable 半字」,后者让表项生效。

#### 4.3.3 源码精读

TLB 的存储数组,本质是几组寄存器数组:

[rtl/peripherals/zipmmu.v:334-339](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L334-L339) — 五个数组:标志、上下文、虚页号、物理页号、有效/已访问位。

地址译码(用 `i_wbs_addr` 两位切出四种访问):

[rtl/peripherals/zipmmu.v:341-350](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L341-L350) — `adr_control/adr_vtable/adr_ptable` 与对应的写使能 `wr_*`。

写入页表项(写 vtable 同时写虚页号、标志、低位上下文;写 ptable 写物理页号、高位上下文,并由下面的 `tlb_valid` 置位):

[rtl/peripherals/zipmmu.v:398-411](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L398-L411) — 写入 TLB 表项的各字段。

[rtl/peripherals/zipmmu.v:448-452](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L448-L452) — **写 ptable 半字才把 `tlb_valid` 置 1**,这是表项生效的触发点。

**控制字**的组装(读时返回:地址宽度、TLB 表项数对数、页大小对数、上下文位数对数,加上低 16 位当前 context)。这些只读字段让软件能查询「这个 MMU 是按什么参数综合出来的」:

[rtl/peripherals/zipmmu.v:455-460](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L455-L460) — 控制字的高 16 位是 4 组只读参数,低 16 位是可写的当前 context。

context 的写入与「是否为零」判断(`z_context` 决定 pass-through):

[rtl/peripherals/zipmmu.v:437-445](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L437-L445) — `kernel_context = (context==0) || (GIE==0)`,即「context 为 0 或在 supervisor 模式」时一律不翻译。

#### 4.3.4 代码实践

**实践目标**:理清「建立一条虚拟→物理映射」需要写哪几个字。

**操作步骤**:

1. 阅读 [rtl/peripherals/zipmmu.v:398-452](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L398-L452) 这段。
2. 假设你想建立映射「context=1, 虚页 0x002 → 物理页 0x020, 可读可写可执行」。回答:
   - 先写哪个地址(vtable 还是 ptable)?分别写什么数据?
   - 哪一步之后这条映射才真正「生效」(`tlb_valid` 被置 1)?

**需要观察的现象**:仅写 vtable 半字不会让表项生效;`tlb_valid` 只在 `wr_ptable` 时被置位。

**预期结果**:建立映射 = 「写控制字设 context」→「写 vtable 半字(虚页号+标志)」→「写 ptable 半字(物理页号+高位上下文,此步生效)」。要删除映射,可写 ptable 把物理页号设为全 1(注释说这代表「页不存在,访问即缺页」),或直接清 valid。

#### 4.3.5 小练习与答案

**练习 1**:控制字的高 16 位为什么要放 4 组「只读参数」?

> 参考答案:因为 MMU 是按综合期参数(`LGTBL`/`PLGPGSZB`/`PLGCTXT`/地址宽度)定制的,不同构建下页大小、表项数都不同。软件无法在运行时知道这些,所以让硬件把它们编码进控制字的高位供软件读取,软件据此才能正确拼装页表项的位字段。

**练习 2**:`tlb_accessed` 位是给谁用的?

> 参考答案:给软件做页面替换(LRU)策略用。硬件在命中并访问某页时自动置位 `tlb_accessed`;软件可以读它判断「哪些页最近被用过」,并在需要腾表项时优先淘汰长期未访问的页。软件也能主动清零它来开始一轮新的统计。

---

### 4.4 缺页与权限错误处理

#### 4.4.1 概念说明

翻译不一定总是成功。`zipmmu` 检测四类错误,它们都在第 3 拍(查表完成后)被判定:

| 错误信号 | 含义 | status_word 中标志 |
|----------|------|--------------------|
| `simple_miss` | 虚页号 + 上下文在表里找不到 | bit0 = 1(page not found) |
| `ro_miss` | 命中了页面,但该页是只读,却试图写 | bit1 = 1(write read-only) |
| `exe_miss` | 命中了页面,但该页不可执行,却试图取指 | bit2 = 1(execute non-exec) |
| `table_err` | 命中了**多个**表项(表损坏,正常不该发生) | bit3 = 1(multiple matches) |

检测到错误后,MMU 会:
1. 把出错虚页号 + 标志写进 `status_word`,供 supervisor 读取定位故障。
2. 拉高 `o_rtn_miss`(缺页)或 `o_rtn_err`(总线错),向上游 CPU 报告,让这次访问停下来。
3. 因为翻译出错时,缺页地址不会再发往下游总线,所以 `o_cyc`/`o_stb` 不会发出,避免用错误的物理地址去打内存。

注意一个重要的设计取舍:`status_word` 的低 4 位全 0 表示「无故障」;任何故障都会被记录,且「写控制字」会清零 status。这是一种「粘性」故障寄存器。

#### 4.4.2 核心流程

错误检测的伪代码(全部在第 3 拍,基于第 2 拍的 `s_tlb_hit`/`s_tlb_miss`):

```
simple_miss = s_pending && s_tlb_miss                     # 一个都没命中
ro_miss     = s_pending && s_tlb_hit && r_we  && ro_flag  # 命中但只读却写
exe_miss    = s_pending && s_tlb_hit && r_exe && !exe_flag# 命中但不可执行却取指
table_err   = s_pending && !s_tlb_miss && !s_tlb_hit      # 命中多个(异常)

if 任一成立:
    status_word <= { 出错虚页号, 0000..., table_err, exe_miss, ro_miss, simple_miss }
    miss_pending<= 1
    o_rtn_miss  <= miss_pending && !bus_pending           # 等下游在途交易清空再上报
```

`o_rtn_miss` 之所以要 `&& !bus_pending`,是要等下游总线上之前已经发出的交易先完成、清空,再上报缺页,避免「上一笔还没回来就报错」的混乱。

#### 4.4.3 源码精读

四类 miss 信号的组合逻辑(全部基于 `s_pending`/`s_tlb_hit`/`s_tlb_miss`):

[rtl/peripherals/zipmmu.v:626-631](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L626-L631) — 四种错误的判定。注意 `simple_miss` 用 `s_tlb_miss`,`table_err` 用「既不 miss 也不 hit」(即命中了多个)。

`status_word` 的写入(出错时锁存虚页号 + 4 位标志;写控制字时清零):

[rtl/peripherals/zipmmu.v:661-671](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L661-L671) — status 寄存器,bit0=page not found、bit1=write RO、bit2=execute non-exec、bit3=multiple matches,与表 4.4.1 对应。

`miss_pending` 与对上游的停顿/缺页上报:

[rtl/peripherals/zipmmu.v:762-774](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L762-L774) — `o_rtn_stall` 在「缺页待处理」时拉高,`o_rtn_miss` 等下游在途交易清空(`!bus_pending`)后才真正上报。

`o_rtn_stall` 的完整条件(把 r_pending 未完成、下游反压、出错都汇总成对 CPU 的反压):

[rtl/peripherals/zipmmu.v:756-760](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L756-L760) — 上游反压的四个来源。

#### 4.4.4 代码实践

**实践目标**:理解一次「写只读页」是如何被拦截并记录的。

**操作步骤**:

1. 假设 supervisor 已建好一条映射:context=2, 虚页 0x003 → 物理页 0x030, 标志 RO=1(只读)。
2. user 模式下(context=2, GIE=1)程序执行一条 `SW` 写虚地址 `0x003xxxxx`(即虚页 0x003)。
3. 走三拍:第 2 拍命中(`s_tlb_hit=1`,因为 vpage 和 context 都对上);第 3 拍检查到 `r_we=1` 且 `ro_flag=1`,于是 `ro_miss=1`。
4. 观察:`status_word` 被写入 `{0x003, 0,0,1,0}`(bit1=1),`o_rtn_miss` 拉高,这次写**不会**发到下游总线。

**需要观察的现象**:下游 `o_cyc`/`o_stb` 不会为这次非法写置位——错误在 MMU 内部就被挡住了,物理内存不会被误写。

**预期结果**:CPU 收到缺页信号,supervisor 读 `status_word` 即可看到「虚页 0x003,写只读页(bit1=1)」的故障记录,进而做相应处理(例如终止该任务)。

#### 4.4.5 小练习与答案

**练习 1**:`table_err`(多个表项同时命中)为什么算错误?它正常会发生吗?

> 参考答案:在一个正确维护的 TLB 里,任意「虚页号 + 上下文」组合最多命中一个表项。如果命中多个,说明页表项之间互相冲突(表被写坏或软件建表出错)。正常情况下不应发生,所以一旦出现就当硬件/软件错误处理,记入 status 的 bit3。

**练习 2**:`o_rtn_miss` 为什么要等到 `!bus_pending` 才上报?

> 参考答案:报缺页前,要保证下游总线上之前已经接受的交易都收到 ack/err、彻底清空。否则「报了缺页让 CPU 重填页表,但下游还有旧交易没回来」会造成返回顺序混乱。`bus_outstanding` 计数器跟踪在途交易数,清零(`bus_pending=0`)后才允许上报。

---

### 4.5 集成现状:为何 DEPRECATED,作者的规划

#### 4.5.1 概念说明

这一节回答「这个 MMU 为什么没在用」。结论是:**它被作者明确标注为 DEPRECATED,没有被当前默认构建启用**。理解这一点非常重要,否则你会误以为 ZipCPU 开箱即用就有虚拟内存。

事情要分三层看:

1. **形式化层面**:它**有**一份完整的形式化证明(`bench/formal/zipmmu.sby`),证明它的内部状态机和总线契约自洽。也就是说「这个模块本身的设计是经过验证的」。
2. **集成层面**:`rtl/zipsystem.v` 里有一段被 `OPT_MMU` 宏保护的实例化代码,把 MMU 接在 CPU 的全局总线(`cpu_gbl_*`)和外部总线之间。但作者在 `rtl/peripherals/README.md` 里明说「这套集成**没有**被测试过,几乎可以肯定还有集成 bug」。
3. **架构层面**:在 ZipCPU 重构以支持多种总线结构(Wishbone / AXI-Lite / AXI)和参数化总线宽度之后,这个 MMU 的接口已经**和新 ZipCore 不匹配**了。文件顶部 Status 段落直接写了 `*DEPRECATED*`。

#### 4.5.2 核心流程(作者的规划)

作者在 README 和源码注释里给出了明确的下一步规划——**把 MMU 重新放到 ZipCore 与总线封装之间**,而不是(像现在这样)放在 ZipSystem 内部、CPU 与外部总线之间:

```
当前(停滞)的集成:              作者规划的重构:
┌─────────────┐                 ┌─────────────┐
│   zipcore   │                 │   zipcore    │  (发出虚地址)
└──────┬──────┘                 └──────┬───────┘
       │ 虚地址                        │ 虚地址
       ▼                              ▼
┌─────────────┐                 ┌─────────────┐
│   zipmmu    │ (在 ZipSystem 内)│   zipmmu    │  ← 重构后放这里
└──────┬──────┘                 └──────┬───────┘
       │ 物理地址                      │ 物理地址
       ▼                              ▼
┌─────────────┐                 ┌─────────────┐
│ icache/dcache│  ← 基于虚地址!  │ icache/dcache│  ← 基于物理地址 ✓
└─────────────┘                 └─────────────┘
```

这个位置变化的**关键收益**是:把 MMU 放在 cache **之前**(更靠近 CPU),翻译发生在 cache 查询**之前**,于是 cache 存储和索引的都是**物理地址**。这解决了作者使用旧 MMU 时遇到的「最大问题之一」——基于虚地址的 cache 在任务切换(context 变化)时必须整块失效,否则会用上一个任务的映射去读缓存内容。

#### 4.5.3 源码精读

文件顶部 Status 段落,明确写出 DEPRECATED 与原因:

[rtl/peripherals/zipmmu.v:182-194](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L182-L194) — 「this MMU now needs to be similarly refactored ... between the ZipCore and its memory access components ... until it does so this MMU implementation should be considered *DEPRECATED*」。

README「Not yet integrated」一节,给出规划与动机(放到 ZipCore 与总线封装之间,让 cache 基于物理地址):

[README.md:99-106](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md#L99-L106) — 作者的计划与「物理地址 cache 能解决一个老大难问题」的说明。

外设 README,说明这是实验性的、仅离线测试过:

[rtl/peripherals/README.md:17-19](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/README.md#L17-L19) — 「an experimental MMU. Has only been tested offline ... integration has not been tested」。

zipsystem.v 中那段「计划中的」集成代码(被 `OPT_MMU` 宏保护,基址 `MMU_ADDR = 8'h80`):

[rtl/zipsystem.v:1584-1610](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1584-L1610) — `zipmmu` 的实例 `themmu`,左侧接配置总线(`sel_mmus`),中间接 CPU 全局总线,右侧接下游外部总线。

形式化证明配置(证明模块自洽,但与「集成可用」是两回事):

[bench/formal/zipmmu.sby:4-15](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/zipmmu.sby#L4-L15) — 用 `mode prove`、深度 23、smtbmc boolector 引擎对 `zipmmu` 做证明。

#### 4.5.4 代码实践

**实践目标**:亲自核验「MMU 未被默认启用」这一结论,而不是只听结论。

**操作步骤**:

1. 在仓库根目录用 git 查找 `OPT_MMU` 是否在默认构建中被定义:
   - 在 `rtl/zipsystem.v` 里搜 `OPT_MMU`,确认它出现在 `` `ifdef `` 里(意味着默认不定义即不编译)。
   - 检查 `rtl/Makefile` 或顶层 `Makefile` 是否在任何默认目标里传了 `-DOPT_MMU`(预期:没有)。
2. 阅读 [README.md:99-118](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md#L99-L118),体会作者把 MMU 与「未来可能的 Linux 移植」放在一起谈的态度——MMU 是一个面向未来、尚未就绪的能力。
3. 对比 [bench/formal/zipmmu.sby](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/zipmmu.sby) 与本讲 4.2 节,确认「形式化证明验证的是翻译状态机的逻辑正确性,而非集成可用性」。

**需要观察的现象**:`OPT_MMU` 是一个「默认关闭、需要显式 `-D` 才打开」的宏;README 把 MMU 列在「Not yet integrated」而非功能列表中。

**预期结果**:你会确认——默认构建的 ZipCPU **没有** MMU;`zipmmu.v` 是一份设计完整、经过形式化验证、但与新总线架构脱节、因此停摆待重构的实验性模块。

> 待本地验证:具体 `OPT_MMU` 是否在某处被传递,取决于你的构建命令行;以上判断基于源码中 `ifdef` 的写法和 README 的归类,请在你的环境里用 `grep -r OPT_MMU` 复核。

#### 4.5.5 小练习与答案

**练习 1**:为什么「把 MMU 放在 cache 之前」能让 cache 基于物理地址?

> 参考答案:如果 MMU 在 CPU 和 cache 之间,CPU 发出的虚地址会先被 MMU 翻译成物理地址,再交给 cache。于是 cache 看到的、存储的全部是物理地址(物理 tag)。任务切换(context 变化)时,同一个物理地址对应的还是同一块内存,cache 内容依然有效,不需要整体失效。这正是作者想解决的问题。

**练习 2**:「有形式化证明」和「集成可用」是一回事吗?

> 参考答案:不是。形式化证明(`zipmmu.sby`)验证的是 `zipmmu` 模块**自身**的状态机和总线契约正确(给定假设下不会出现非法状态)。但「集成」涉及 zipmmu 与 ZipCore、cache、外部总线、中断处理的对接是否正确、时序是否收敛、是否覆盖所有边界情形——这些是模块级证明管不到的。所以作者才会说「模块证明过了,但集成没测试过,肯定还有 bug」。

---

## 5. 综合实践

把本讲的知识串起来,完成下面这个「纸面设计 + 源码核对」任务。

**任务背景**:假设你是 ZipCPU 的二次开发者,想在重构后让一个 user 任务拥有两段独立的虚拟内存区域:代码区(可执行、只读)和数据区(可读写、不可执行)。

**要求**:

1. **规划映射**:用本讲的参数默认值(页大小 \(2^{20}\) 字节、64 个 TLB 表项),为该任务(context=3)设计两条映射:
   - 虚页 `0x010` → 物理页 `0x100`,标志:EXE=1, RO=1(可执行只读代码区)。
   - 虚页 `0x020` → 物理页 `0x200`,标志:RO=0(可读写数据区)。
2. **写出配置序列**:参考 4.3 节,写出 supervisor 要通过配置总线执行的写入序列——先写控制字设 context,再依次写每条映射的 vtable 半字和 ptable 半字。标注每一步 `i_wbs_addr[LGTBL+1,0]` 取值和数据内容。
3. **预测一次访问**:任务执行时取指虚页 `0x010`(命中代码区),应当走通;若它试图**写**虚页 `0x010`,应当触发哪一类 miss?`status_word` 低 4 位是什么?参考 4.4 节核对。
4. **核对源码**:回到 [rtl/peripherals/zipmmu.v:398-452](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L398-L452) 与 [rtl/peripherals/zipmmu.v:626-631](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v#L626-L631),确认你的写入序列确实能让两条表项 `tlb_valid` 置位、且你预测的 miss 类型与源码判定逻辑一致。
5. **反思集成**:最后用一两句话说明,这个设计在当前默认 ZipCPU 上为什么跑不起来(提示:4.5 节)。

**预期结果**:

- 配置序列应包含「写控制字(context=3)」+ 两条「写 vtable → 写 ptable」。
- 写虚页 `0x010`(代码区、RO=1)会触发 `ro_miss`,`status_word` 低 4 位 = `4'b0010`(bit1=1)。
- 在当前默认构建中跑不起来,因为 `OPT_MMU` 默认未定义、且 MMU 已被标注 DEPRECATED、与新 ZipCore 总线接口不匹配。

---

## 6. 本讲小结

- `zipmmu` 是一个 **「bump-in-the-line」夹心层 MMU**:用一条配置总线(slave)接受 supervisor 配置,改写另一条数据总线(master)上的地址;两条总线约定不同时活跃。
- 翻译用**片上全相连 TLB**(默认 64 项)实现,没有内存页表往返。翻译分**三拍**:锁存请求 → 并行比较所有表项得命中下标 → 用下标读物理页号并检查权限。
- 同一页连续访问靠 `last_page_valid` 缓存降到 1 拍;supervisor 模式(context=0 或 GIE=0)直接走物理地址,不翻译。
- 页表项由虚页号、物理页号、上下文、4 位权限标志(RO/EXE/CH/AX)组成;**写 ptable 半字才让表项 `tlb_valid` 生效**。控制字高 16 位编码综合期参数,状态字低 4 位记录缺页原因。
- 四类错误(`simple_miss`/`ro_miss`/`exe_miss`/`table_err`)在第 3 拍判定,记入 status 并以 `o_rtn_miss`/`o_rtn_err` 上报,等下游在途交易清空后才报。
- **当前状态:DEPRECATED**。模块本身有形式化证明,但 `zipsystem.v` 里的 `OPT_MMU` 集成未测试,且接口与新 ZipCore 多总线架构不匹配。作者计划把 MMU 重构到 **ZipCore 与总线封装之间**,使 cache 基于物理地址。

---

## 7. 下一步学习建议

- **想看「真正在用」的总线胶水与外设**:回到 u4-l4(总线支持模块 rtl/ex)和 u4-l5(外设),那些才是当前默认构建里的部件,可以作为「已集成」的对照参考。
- **想理解为什么 cache 与虚地址有冲突**:复习 u3-l2(取指模块族 pfcache)和 u3-l6(访存模块族 dcache),观察 pfcache/dcache 的 tag 是基于什么地址的,你就能体会「物理地址 cache」为什么是作者的目标。
- **想了解 MMU 的形式化证明怎么跑**:进入 u5-l2(形式化验证体系),那里会讲 `bench/formal` 下的 `.sby` 配置与 `fwb_master/fwb_slave` 属性封装——本讲的 `zipmmu.sby` 正是其中一例。
- **想动手做集成尝试(高风险)**:可以阅读 [rtl/zipsystem.v:1584-1610](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1584-L1610) 的 `OPT_MMU` 实例,理解它的接法,但请记住作者明说这里还有未发现的集成 bug,不建议作为生产用途。
