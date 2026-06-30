# axi_iw_converter：ID 宽度转换

> 承接：[u10-l1 axi_id_remap](u10-l1-axi-id-remap.md)（宽 ID → 窄 ID，**保留独立性**）、[u10-l2 axi_id_serialize](u10-l2-axi-id-serialize.md)（宽 ID → 窄 ID，**放弃独立性**）、[u4-l2 axi_id_prepend](u4-l2-modify-addr-id-prepend.md)（ID 高位前置/剥离，用于窄→宽的零扩展）。本讲把这三块积木收进**一个统一入口**，回答一个工程问题：当我面对任意两个 ID 宽度不同的子网时，到底该用哪一个？

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `axi_iw_converter` 自身**几乎不写逻辑**：它是一个纯「分发器」，按 slave/master 两端 ID 宽度的关系，从 `axi_id_remap`、`axi_id_serialize`、`axi_id_prepend`、直通 四种实现里**编译期选一种**。
- 画出那条三分支（外加直通）的决策树，并指出每条分支成立的判据。
- 看懂它为什么位于编译层级 **Level 4**——这个数字正是「它只是更底层积木的组合」的直接证据。
- 理解它那一长串 `Max*` 参数并非冗余，而是「按分支条件**生效**」的：哪些参数喂给 remap、哪些喂给 serialize。
- 读懂测试台 `tb_axi_iw_converter` 如何用一个**参数矩阵**同时覆盖三条分支，并用「除 ID 外逐字段相等」断言验证响应正确回送。

## 2. 前置知识

### 2.1 同一个问题，三种答案

U10 这一单元始终在回答同一个问题：**两端的 AXI ID 宽度不一致，怎么办？** 但「不一致」其实有三种姿态，对应三种最省事的做法：

| 关系 | 含义 | 最省事的做法 |
|------|------|--------------|
| master 端 ID **更宽** | 下游能容纳更多 ID 位 | 把 slave 端 ID **高位补零**扩到 master 宽度即可，无需任何翻译 |
| master 端 ID **更窄**，但仍够放下所有在途 ID | 下游 ID 位少，但「不同 slave ID 还能落到不同 master ID」 | 用 `axi_id_remap` 重映射，**保留独立性** |
| master 端 ID **更窄**，且放不下所有在途 ID | 必有两个 slave ID 挤进同一 master ID | 用 `axi_id_serialize`，**放弃独立性**换更窄 ID |
| 两端 ID 宽度**相等** | 类型完全一致 | 一条 `assign` 直通 |

`axi_iw_converter` 的全部价值，就是**把这四种情形打包成一个对外统一的模块**：调用方只要给出两端的 ID 宽度，它自动挑出对应实现。这正是「组合优于配置」哲学（见 [u1-l1](u1-l1-project-overview.md)）的又一活样本——它不发明新机制，只做正确的分发。

### 2.2 为什么需要「统一入口」

设想你在一个异构片上网络里，要把一段宽 ID 的子网（比如某 CPU 簇）连到一段窄 ID 的子网（比如某个外设桥）。你不想在顶层手写 `if (宽 > 窄) 例化 remap; else ...` 的判断，因为：

- 参数稍变，分支就可能切换，手写容易漏改；
- 顶层只想面对一个固定名字的模块，便于脚本化和复用。

`axi_iw_converter` 用 `generate` 把这个判断**搬进模块内部**，对外只暴露「给两端 ID 宽度 + 几个容量上限」的简单参数面。这也是它在 [u15-l4 异构网络实战](u15-l4-heterogeneous-network.md) 里被当作「ID 子网胶水」反复使用的原因。

### 2.3 Level 4 不是随便给的

回顾 [u1-l2](u1-l2-repo-and-build.md) 的编译层级规则：一个文件的层级 = 它在本包内最长依赖链 + 1。`axi_iw_converter` 在 `Bender.yml` 里被标为 **Level 4**，而它例化的三块积木分别是：

- `axi_id_prepend` —— Level 2
- `axi_id_remap` —— Level 2
- `axi_id_serialize` —— Level 3（它自己又例化了 `axi_demux`/`axi_mux`/`axi_serializer`/`axi_id_prepend`）

于是 \(\text{Level}(\text{iw\_converter}) = \max(2, 2, 3) + 1 = 4\)。**这个 4 直接告诉我们：本模块不引入任何新的协议逻辑，它纯粹是把三个更底层的翻译器按宽度关系重新组合。** 阅读时要带着这个预期——别在里面找算法，算法都在子模块里。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/axi_iw_converter.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv) | 本讲主角。结构体内核 `axi_iw_converter`（三分支 `generate`）+ 接口外壳 `axi_iw_converter_intf`。 |
| [src/axi_id_remap.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_remap.sv) | 宽→窄且**保留独立性**的翻译器（u10-l1 已精读），`gen_remap` 分支例化它。 |
| [src/axi_id_serialize.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_serialize.sv) | 宽→窄且**放弃独立性**的翻译器（u10-l2 已精读），`gen_serialize` 分支例化它。 |
| [src/axi_id_prepend.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_id_prepend.sv) | ID 高位前置/剥离器（u4-l2 已精读），`gen_upsize` 分支借它做零扩展。 |
| [test/tb_axi_iw_converter.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_iw_converter.sv) | 唯一直接验证本模块的测试台，用参数矩阵覆盖三条分支。 |
| [scripts/run_vsim.sh](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh) | 回归脚本，定义了那个覆盖三条分支的参数扫描矩阵。 |

## 4. 核心概念与源码讲解

### 4.1 模块定位与三分支决策

#### 4.1.1 概念说明

`axi_iw_converter` 的模块头文档把它的「分发器」身份讲得很明白：任意两种 ID 宽度组合都合法；宽→窄时有**两种**降宽方案（remap 或 serialize），其余情形（窄→宽、等宽）则分别走「补零」和「直通」。

关键直觉：**它只看两个数——两端 ID 宽度的大小关系，以及「在途唯一 ID 数」是否塞得下——就足以决定用哪块积木。** 这是一个纯粹的编译期分类问题，没有任何运行期状态。

#### 4.1.2 核心流程

模块主体是一条「四级 `if / else if / else`」的 `generate` 决策树，按下面顺序判定（先满足者生效）：

```text
                   ┌─ AxiMstPortIdWidth < AxiSlvPortIdWidth ?  (宽→窄，需降宽)
                   │     ├─ AxiSlvPortMaxUniqIds ≤ 2^AxiMstPortIdWidth ?
                   │     │     是 → gen_remap     : 例化 axi_id_remap     (保留独立性)
                   │     │     否 → gen_serialize : 例化 axi_id_serialize (放弃独立性)
                   │     └─（构成 gen_downsize 块）
                   ├─ AxiMstPortIdWidth > AxiSlvPortIdWidth ?  (窄→宽)
                   │     是 → gen_upsize      : 例化 axi_id_prepend (NoBus=1, pre_id='0 高位补零)
                   └─ 其余（两端等宽）
                         → gen_passthrough  : assign 直通
```

注意第一级的判定用的是**严格小于** `<`：只有 master 端真的更窄时才进入「降宽」子树；只要 master 端不更窄（即大于或等于），就分别走补零或直通。降宽子树内部的第二次判定，用的判据正是 u10-l1 / u10-l2 反复强调的那条线——「下游窄 ID 能否装下所有在途的唯一 slave ID」。

\[ \text{判据} = \big(\,\text{AxiSlvPortMaxUniqIds} \;\le\; 2^{\text{AxiMstPortIdWidth}}\,\big) \]

左边是上游**承诺**的在途唯一 ID 数上限，右边是下游 ID 空间**实际**能表达的 ID 数。能装下→保留独立性（remap）；装不下→只能放弃独立性（serialize）。

#### 4.1.3 源码精读

模块头文档先给总纲，再分述两种降宽方案，措辞与上述决策树一一对应：

[src/axi_iw_converter.sv:18-43](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L18-L43) —— 模块定位说明：任意两端 ID 宽度组合都合法；master 端更宽或相等时「补零」即可，master 端更窄时则按「在途唯一 ID 数」在 remap 与 serialize 间二选一。

决策树本体在 `generate` 块里。第一级判定 master 端是否更窄，进入 `gen_downsize`：

[src/axi_iw_converter.sv:127-146](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L127-L146) —— `gen_downsize` 块：master 端 ID 更窄。内部再用 `AxiSlvPortMaxUniqIds <= 2**AxiMstPortIdWidth` 判定，命中 `gen_remap` 即例化 `axi_id_remap`，把 `AxiSlvPortMaxUniqIds` 与 `AxiSlvPortMaxTxnsPerId` 两个参数喂下去。

不命中则走 `gen_serialize`：

[src/axi_iw_converter.sv:146-168](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L146-L168) —— `gen_serialize` 块：例化 `axi_id_serialize`，换喂 `AxiSlvPortMaxTxns`、`AxiMstPortMaxUniqIds`、`AxiMstPortMaxTxnsPerId` 三个参数（与 remap 分支喂的参数不同，详见 4.2）。

第二级，master 端更宽时进入 `gen_upsize`，借用 `axi_id_prepend` 做「高位补零」：

[src/axi_iw_converter.sv:169-216](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L169-L216) —— `gen_upsize` 块：例化 `axi_id_prepend`，关键两处是 `.NoBus(32'd1)`（单总线）与 `.pre_id_i('0)`（前置全零标签）。这正是「窄→宽只需高位补零」的物理实现，与 u4-l2 讲的 `id_prepend` 机制完全一致。

第三级，两端等宽时直通：

[src/axi_iw_converter.sv:217-220](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L217-L220) —— `gen_passthrough` 块：两条 `assign` 把请求/响应结构体整体搬过去。之所以能整体赋值，是因为两端 ID 宽度相等时 `slv_req_t` 与 `mst_req_t` 是**同一类型**。

> 小贴士：对比 upsize 与 passthrough 就能看明白「为什么 upsize 不能也用 `assign`」——两端口 ID 宽度不同时，`slv_req_t` 与 `mst_req_t` 是**不同类型**，SystemVerilog 不允许整体赋值，必须靠 `axi_id_prepend` 做字段级搬运。

#### 4.1.4 代码实践

**实践目标**：用「读源码 + 查参数」的方式，亲手确认三种参数组合各走哪条分支。

1. 打开 [src/axi_iw_converter.sv:127-220](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L127-L220)，对照决策树。
2. 对下面三组参数，分别判断会进入哪个 `generate` 块、例化哪个子模块：

   | 组 | AxiSlvPortIdWidth | AxiMstPortIdWidth | AxiSlvPortMaxUniqIds |
   |----|-------------------|-------------------|----------------------|
   | A | 4                 | 2                 | 3                    |
   | B | 4                 | 2                 | 8                    |
   | C | 2                 | 4                 | （无关）             |

3. **需要观察的现象**：A 组里 \(3 \le 2^2=4\) 成立→`gen_remap`；B 组里 \(8 > 4\)→`gen_serialize`；C 组 master 更宽→`gen_upsize`。
4. **预期结果**：A 例化 `axi_id_remap`，B 例化 `axi_id_serialize`，C 例化 `axi_id_prepend`（`NoBus=1`）。
5. 实际是否在仿真器里展开成你预期的实例，**待本地验证**（可用 `vlog -lint ...; vsim -voptargs="+acc" ...` 后在层次窗口里查看 `i_axi_*` 实例名）。

#### 4.1.5 小练习与答案

**练习 1**：若 `AxiSlvPortIdWidth == AxiMstPortIdWidth`，模块里会综合出任何逻辑吗？

> **答案**：不会。此时走 `gen_passthrough`，只剩两条 `assign`，等价于一根导线，零逻辑零延迟。

**练习 2**：把 `AxiSlvPortMaxUniqIds` 设成正好等于 \(2^{\text{AxiMstPortIdWidth}}\)，会走 remap 还是 serialize？

> **答案**：走 remap。判据用的是 `<=`（小于等于），见 [L128](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L128)。恰好塞得下时仍保留独立性。

### 4.2 参数矩阵：按分支「条件生效」的容量上限

#### 4.2.1 概念说明

一眼看过去，`axi_iw_converter` 的参数表有一长串 `Max*`：`AxiSlvPortMaxUniqIds`、`AxiSlvPortMaxTxnsPerId`、`AxiSlvPortMaxTxns`、`AxiMstPortMaxUniqIds`、`AxiMstPortMaxTxnsPerId`。初学者常疑惑「为什么这么多、我到底要填哪些」。

关键直觉：**这些参数不是全都要用，而是按你落入哪条分支，只有其中几个会被传给子模块。** 也就是说，参数表是「四条分支所需参数的并集」，每条分支只取自己那几个。模块头文档在每个参数的注释里都写明了「只在……情形下生效」。

#### 4.2.2 核心流程

把参数按「喂给谁」分组：

| 参数 | 含义 | 谁用它 |
|------|------|--------|
| `AxiSlvPortIdWidth` / `AxiMstPortIdWidth` | 两端 ID 位宽 | 全部分支都用（决策树本身的判据） |
| `AxiSlvPortMaxUniqIds` | slave 端在途唯一 ID 上限 | 决策树判据；remap/serialize 都传 |
| `AxiSlvPortMaxTxnsPerId` | slave 端每 ID 在途事务上限 | **仅 remap**（作为 `AxiMaxTxnsPerId`） |
| `AxiSlvPortMaxTxns` | slave 端在途事务总数上限 | **仅 serialize** |
| `AxiMstPortMaxUniqIds` | master 端在途唯一 ID 上限 | **仅 serialize** |
| `AxiMstPortMaxTxnsPerId` | master 端每 ID 在途事务上限 | **仅 serialize** |
| `AxiAddrWidth` / `AxiDataWidth` / `AxiUserWidth` | 地址/数据/用户位宽 | 全部分支（且两端必须相等） |

因此填写参数时，先按 4.1 判断落入哪条分支，再只关心那条分支需要的几个容量上限即可，其余填 0 不会影响该分支。

#### 4.2.3 源码精读

每个 `Max*` 参数的文档注释都点明了它的「生效条件」。以 remap 专用参数为例：

[src/axi_iw_converter.sv:56-61](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L56-L61) —— `AxiSlvPortMaxTxnsPerId` 注释明写「仅在 `AxiSlvPortMaxUniqIds <= 2**AxiMstPortIdWidth` 时生效，并作为 `AxiMaxTxnsPerId` 传给 `axi_id_remap`」。

serialize 专用的三个参数同样如此：

[src/axi_iw_converter.sv:62-81](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L62-L81) —— `AxiSlvPortMaxTxns`、`AxiMstPortMaxUniqIds`、`AxiMstPortMaxTxnsPerId` 三个参数都标注「仅在 `AxiSlvPortMaxUniqIds > 2**AxiMstPortIdWidth` 时生效」，分别对应 `axi_id_serialize` 的同名参数。

这份「条件生效」的约定还被**断言**固化下来，防止调用方填错：

[src/axi_iw_converter.sv:235-243](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L235-L243) —— 编译期断言：若落入 remap 分支，强制 `AxiSlvPortMaxTxnsPerId > 0`；若落入 serialize 分支，强制 `AxiMstPortMaxUniqIds > 0` 且 `AxiMstPortMaxTxnsPerId > 0`。填 0 会在 elaborate 阶段 `$fatal`。

此外，一组断言锁死了「本模块只改 ID 宽度、不改其它宽度」的契约：

[src/axi_iw_converter.sv:244-251](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L244-L251) —— 断言两端 AW 地址、W 数据、AR 地址、R 数据位宽必须相等。换句话说，`axi_iw_converter` 只管 ID 宽度，**数据宽度转换是 `axi_dw_converter`（u11）的职责**，两者职责正交。

#### 4.2.4 代码实践

**实践目标**：亲手触发 4.2.3 里的断言，体会「填错参数即 elaborate 失败」的保护。

1. 复制一份 `tb_axi_iw_converter`（或写一个最小顶层），把参数设成 serialize 分支（如 `SlvIdWidth=4, MstIdWidth=2, SlvMaxUniqIds=8`），但故意把 `AxiMstPortMaxUniqIds` 留成 `0`。
2. 用 `make compile.log`（或直接 `vsim` elaborate）加载它。
3. **需要观察的现象**：elaborate 阶段就报 `$fatal(1, "Parameter AxiMstPortMaxUniqIds has to be larger than 0!")`，仿真根本跑不起来。
4. **预期结果**：把该参数改成 `>0` 后断言通过、能正常仿真。
5. 该断言触发与否，**待本地验证**（取决于你能否搭建一个最小 elaborate 环境）。

#### 4.2.5 小练习与答案

**练习 1**：某同事在一个走 `gen_upsize` 分支的例化里，把 `AxiSlvPortMaxTxnsPerId` 填成了 0，会出错吗？

> **答案**：不会。该参数仅 remap 分支生效，upsize 分支根本不读它，断言也不检查（断言只在 `AxiSlvPortMaxUniqIds <= 2**AxiMstPortIdWidth` 时才查它）。填 0 无害。

**练习 2**：为什么 `AxiSlvPortMaxUniqIds` 同时出现在决策判据和子模块参数里，而其它 `Max*` 只在子模块里用？

> **答案**：因为它身兼两职——既是「能否塞得下」的判据（决定 remap 还是 serialize），又是 remap/serialize 内部计数器容量的输入。其它 `Max*` 只描述容量，不参与分支选择。

### 4.3 接口外壳 axi_iw_converter_intf

#### 4.3.1 概念说明

像库内大多数模块一样，`axi_iw_converter` 有两个版本：**结构体内核**（`req_t`/`resp_t` 端口，参数化、可综合、便于在 datapath 里嵌套）与**接口外壳** `axi_iw_converter_intf`（用 `AXI_BUS.Slave`/`AXI_BUS.Master` 端口，便于在顶层/测试台里直接连接口）。这层「接口外壳 + 结构体内核」范式见 [u2-l4](u2-l4-typedef-assign-port-macros.md)。

外壳的职责单一：**把 `AXI_BUS` 接口的扁平信号与结构体之间互连**，自身不含任何业务逻辑，只是把请求/响应在两种表达之间搬运，再调一次内核。

#### 4.3.2 核心流程

外壳内部做三件事：

```text
1. 用 `AXI_TYPEDEF_*` 宏，按两端各自的 ID 宽度，声明 slv_req_t/slv_resp_t 与 mst_req_t/mst_resp_t；
2. 用 `AXI_ASSIGN_TO_REQ / FROM_RESP / FROM_REQ / TO_RESP` 四个宏，在 AXI_BUS 接口与结构体之间搬运；
3. 例化结构体内核 axi_iw_converter，把四组结构体接上去。
```

注意两端声明的是**两套**类型——`slv_*_t` 用 `AXI_SLV_PORT_ID_WIDTH`、`mst_*_t` 用 `AXI_MST_PORT_ID_WIDTH`。这正是「两端 ID 宽度可以不同」在外壳层的体现。

#### 4.3.3 源码精读

外壳的端口只暴露两个接口与 clk/rst：

[src/axi_iw_converter.sv:274-279](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L274-L279) —— `axi_iw_converter_intf` 端口：`slv` 为 `AXI_BUS.Slave`、`mst` 为 `AXI_BUS.Master`。两个接口可以用不同的 `AXI_ID_WIDTH` 参数化。

用宏声明两套结构体类型：

[src/axi_iw_converter.sv:287-301](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L287-L301) —— 分别用 `slv_id_t` 与 `mst_id_t` 声明 slave/master 两端的通道与 req/resp 结构体；两套类型只有 `id` 字段位宽不同。

四个搬运宏 + 例化内核：

[src/axi_iw_converter.sv:308-335](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L308-L335) —— `AXI_ASSIGN_TO_REQ/FROM_RESP` 桥接 slave 接口与 `slv_req/slv_resp`；`AXI_ASSIGN_FROM_REQ/TO_RESP` 桥接 master 接口与 `mst_req/mst_resp`；中间例化结构体内核 `i_axi_iw_converter`。外壳零逻辑。

外壳还自带一组接口宽度一致性断言，确保调用方传入的 `AXI_BUS` 实际位宽与参数声明吻合：

[src/axi_iw_converter.sv:337-348](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L337-L348) —— 断言 `slv.AXI_ID_WIDTH == AXI_SLV_PORT_ID_WIDTH` 等，防止「接口位宽与参数不一致」这类低级连错。

#### 4.3.4 代码实践

**实践目标**：验证「外壳只是搬运、内核才是大脑」。

1. 阅读 [src/axi_iw_converter.sv:303-335](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L303-L335)，数一数外壳里有没有任何 `always_ff` / 状态机 / 计数器。
2. **需要观察的现象**：除了 `assign`（经宏展开）与一个内核实例，外壳里没有任何时序逻辑。
3. **预期结果**：确认外壳的 RTL 综合后只是一层连线 + 内核，自身面积开销为零。
4. 这一点在源码上即可确认，无需运行仿真。

#### 4.3.5 小练习与答案

**练习 1**：为什么外壳要声明**两套** `req_t`/`resp_t`，而不是一套？

> **答案**：因为两端 ID 宽度可以不同，导致 `slv_*_t` 与 `mst_*_t` 是不同类型（`id` 字段位宽不同）。一套类型无法同时匹配两个不同 ID 宽度的接口。

**练习 2**：如果我想在纯结构体环境（不用 `AXI_BUS` 接口）里用 ID 宽度转换，该用内核还是外壳？

> **答案**：用内核 `axi_iw_converter`，直接把 `req_t`/`resp_t` 接上。外壳是为接口环境准备的，在结构体环境里反而是多余的搬运层。

### 4.4 测试台 tb_axi_iw_converter：用一个矩阵覆盖三条分支

#### 4.4.1 概念说明

`axi_iw_converter` 没有针对每条分支的独立小测试台——它**只有一个** `tb_axi_iw_converter`，靠**参数化 + 回归矩阵**同时验证 remap、serialize、upsize、passthrough 四种实现。这和 u10-l2 提到的「`axi_id_serialize` 没有专属 TB，靠本 TB 间接覆盖」是同一件事的两面：本 TB 是整个 U10 单元的统一验证入口。

测试台的核心思想是「**不假设 ID 怎么变，只假设除 ID 外其它字段不变**」。它在 upstream（slave 端口侧）和 downstream（master 端口侧）各挂一个监听器，把每一拍握手记下来，然后断言「同一笔事务在两端除 `id` 外逐字段相等」。这样无论内部走 remap 还是 serialize，检查逻辑都不用改。

#### 4.4.2 核心流程

测试台由四个并发块构成（与 [u3-l3](u3-l3-write-a-testbench.md) 讲的 TB 骨架一致）：

```text
1. 时钟复位发生器 clk_rst_gen；
2. 激励：rand_axi_master（ID 宽度 = slave 端）挂在 upstream，发 TbNumReadTxns 读 + TbNumWriteTxns 写；
3. 响应端：rand_axi_slave（ID 宽度 = master 端）挂在 downstream，做随机回包；
4. 自检：一个 initial 进程持续监听两端，按「除 ID 外相等」断言逐拍比对；
   仿真结束后再断言所有队列排空（没有事务被丢弃）。
```

关键技巧：master 用 `TbAxiSlvPortIdWidth`、slave 用 `TbAxiMstPortIdWidth`，于是两端的 ID 宽度天然不同，正好驱动 DUT 的某一条分支。改变这两个参数，就切换被测分支。

#### 4.4.3 源码精读

激励端与响应端的 ID 宽度故意取不同：

[test/tb_axi_iw_converter.sv:66-90](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_iw_converter.sv#L66-L90) —— `rand_axi_master_t` 的 `.IW(TbAxiSlvPortIdWidth)`、`rand_axi_slave_t` 的 `.IW(TbAxiMstPortIdWidth)`。两端 ID 宽度不同，DUT 才有活干。

DUT 用接口外壳例化，两端分别接 upstream/downstream 接口：

[test/tb_axi_iw_converter.sv:164-180](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_iw_converter.sv#L164-L180) —— `i_dut` 是 `axi_iw_converter_intf`，`.slv(axi_upstream)`、`.mst(axi_downstream)`，把全部 `Tb*` 参数透传下去。

自检的「除 ID 外相等」由一个辅助类实现：

[test/tb_axi_iw_converter.sv:18-38](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_iw_converter.sv#L18-L38) —— `tb_iw_converter_ax` 类的 `equals_except_id`：先把对方 beat 的 `id` 改成自己的 `id`，再用结构体逐字段 `==` 比较。这正实现了「除 id 外全相等」的判据，与「模块只改 id 不改其它字段」的契约（4.2.3 断言）遥相呼应。

监听逻辑在 downstream 抓到 AW/AR 时调用该判据：

[test/tb_axi_iw_converter.sv:323-358](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_iw_converter.sv#L323-L358) —— downstream AW 监听：按地址找回 upstream 那笔 AW，断言 `equals_except_id` 成立（除 id 外逐字段相等）；并额外断言「同一 upstream id 的所有事务都映射到同一 downstream id」——这条正好刻画了 remap/serialize 都必须满足的「同 upstream id → 同 downstream id」映射稳定性。

仿真结束时的「排空检查」确保没有事务丢失：

[test/tb_axi_iw_converter.sv:411-441](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_iw_converter.sv#L411-L441) —— 遍历所有按 id 建立的队列，断言每个队列都已清空（`size()==0`），即所有 upstream 发出的事务都收到了响应。

真正驱动「四条分支都被覆盖」的是回归脚本的参数矩阵：

[scripts/run_vsim.sh:91-146](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L91-L146) —— `axi_iw_converter` 分支：双层循环扫 `SLV_PORT_IW ∈ {1,2,3,4,8}` 与 `MST_PORT_IW ∈ {1,2,3,4}`，并对每组组合再扫若干 `MAX_UNIQ_SLV_PORT_IDS`。

矩阵内部正是用 4.1 的判据分流到 remap / serialize 两套不同参数：

[scripts/run_vsim.sh:108-132](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L108-L132) —— `if MST_PORT_IW < SLV_PORT_IW`（降宽）时，再用 `if MAX_UNIQ_SLV_PORT_IDS <= MAX_MST_PORT_IDS` 区分：命中则传 `-GTbAxiSlvPortMaxTxnsPerId=5`（remap 参数），否则传 `-GTbAxiSlvPortMaxTxns=31 -GTbAxiMstPortMaxUniqIds=... -GTbAxiMstPortMaxTxnsPerId=7`（serialize 参数）。这条 `if` 与模块内的 `generate` 判据**完全同构**——脚本作者特意复刻了 DUT 的分支逻辑，保证两条降宽分支都被真实命中。

#### 4.4.4 代码实践

**实践目标**：跑通完整回归矩阵，确认四条分支都过。

1. 在仓库根目录执行 `make sim-axi_iw_converter.log`（见 [Makefile:29](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/Makefile#L29)，TB 已在 `TBS` 列表里）。
2. 该目标会调用 `scripts/run_vsim.sh` 的 `axi_iw_converter` 分支，对大量参数组合各跑若干随机种子（[u1-l4](u1-l4-compile-sim-synth.md) 讲过的 directed random regression）。
3. **需要观察的现象**：日志里每组参数组合都打印 `Errors: 0,`；若有任意一组失败，Makefile 会因 `grep "Error:"` 兜底而判负。
4. **预期结果**：全部组合通过，证明 remap / serialize / upsize / passthrough 四条分支在随机事务下都正确回送响应。
5. 该回归耗时较长（组合多），**待本地验证**（需要 QuestaSim/vsim 环境）；若只想快速验证单组，可直接用 [L113-120](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L113-L120) 里那行 `call_vsim` 手动跑一次。

#### 4.4.5 小练习与答案

**练习 1**：为什么测试台不直接断言「downstream id 等于某个固定函数(upstream id)」？

> **答案**：因为映射规则随分支而变（remap 是动态分配、serialize 是按桶映射），且 remap 的具体分配还有运行期随机性。TB 只断言更弱但永远成立的不变式：「除 id 外逐字段相等」+「同 upstream id 映射到同 downstream id」，这足以抓住任何错配/丢字段 bug，又不耦合到具体映射算法。

**练习 2**：`run_vsim.sh` 里那条 `if [ $MAX_UNIQ_SLV_PORT_IDS -le $MAX_MST_PORT_IDS ]` 如果删掉、统一只用一套参数会怎样？

> **答案**：会漏掉其中一条降宽分支的覆盖。例如只用 remap 参数去跑 serialize 组合，`AxiMstPortMaxUniqIds` 等不满足，要么断言 `$fatal`、要么 serialize 分支行为不被真正压测。这条 `if` 是「让矩阵与 DUT 分支同构」的关键。

## 5. 综合实践

把本讲三块知识串起来：**追踪一次真实例化里到底 instantiated 了哪个子模块，并验证响应正确回送。**

**任务**：搭建一个「宽 ID 主域 → `axi_iw_converter` → 窄 ID 从域」的最小拓扑，强制落入 `gen_serialize` 分支，并确认行为。

1. **配置参数**（让 master 端 ID 比上游更窄、且塞不下）：取 `TbAxiSlvPortIdWidth=4`、`TbAxiMstPortIdWidth=2`、`TbAxiSlvPortMaxUniqIds=8`（因为 \(8 > 2^2=4\)，必走 serialize）。
2. **追踪内部实例**：根据 4.1.2 的决策树，确认 DUT 内部例化的是 `i_axi_serialize`（而非 `i_axi_id_remap`）。可对照 [src/axi_iw_converter.sv:146-168](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L146-L168)；若在仿真器里，展开 `i_dut.i_axi_iw_converter.gen_downsize.gen_serialize.i_axi_id_serialize` 层次进一步还能看到 u10-l2 讲过的 `axi_demux`/`axi_serializer`/`axi_mux` 三段。
3. **跑回归**：用第 2 步参数执行 `make sim-axi_iw_converter.log`（或参照 [run_vsim.sh:122-131](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/scripts/run_vsim.sh#L122-L131) 的 serialize 分支手动 `call_vsim`）。
4. **验证响应回送**：观察日志出现 `Errors: 0,`，且 [test/tb_axi_iw_converter.sv:411-441](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_iw_converter.sv#L411-L441) 的排空断言不报错——这意味着上游发出的所有读/写事务，其响应都带着**还原后的原 upstream id** 正确回到上游。
5. **思考题（可选）**：把参数改成 `TbAxiSlvPortMaxUniqIds=3`（\(3 \le 4\)，改走 remap），重跑一次。对比两次仿真里 DUT 内部实例名（`i_axi_id_remap` vs `i_axi_serialize`），体会「参数一变、底层实现就换」的统一入口价值。

> 说明：本实践依赖 QuestaSim/vsim 与 Bender 环境；若本地不具备，可退化为「源码阅读型实践」——只完成第 1、2 步的决策追踪与层次对照，不实际仿真。

## 6. 本讲小结

- `axi_iw_converter` 自身**几乎不写逻辑**，是一个编译期「分发器」：按两端 ID 宽度关系，从 `axi_id_remap`、`axi_id_serialize`、`axi_id_prepend`、直通 四种实现里选一种。
- 决策树两级判定：先看 master 端是否更窄（`<`），再看 \( \text{AxiSlvPortMaxUniqIds} \le 2^{\text{AxiMstPortIdWidth}} \) 决定 remap 还是 serialize；master 端更宽则用 `axi_id_prepend` 高位补零；等宽则 `assign` 直通。
- 那一长串 `Max*` 参数是「四条分支所需参数的并集」，每条分支只取自己那几个；断言会强制对应分支的关键参数 `>0`，并锁死「只改 ID 宽度、不改 addr/data/user 宽度」的契约。
- 编译层级 **Level 4** 是「纯组合」的直接证据：\(\text{Level} = \max(\text{id\_remap}=2,\ \text{id\_serialize}=3,\ \text{id\_prepend}=2)+1 = 4\)，本模块不引入任何新协议逻辑。
- 接口外壳 `axi_iw_converter_intf` 零逻辑，只负责在 `AXI_BUS` 接口与两套（ID 宽度不同的）结构体之间搬运；结构体环境应直接用内核。
- 唯一的测试台 `tb_axi_iw_converter` 用「除 ID 外逐字段相等」+「同 upstream id → 同 downstream id」两条不变式自检，靠 `run_vsim.sh` 的参数矩阵（其 `if` 与 DUT 分支同构）覆盖 remap/serialize/upsize/passthrough 全部分支。

## 7. 下一步学习建议

- 横向对比数据宽度转换：[u11-l3 axi_dw_converter](u11-l3-dw-converter.md) 是「ID 宽度不动、数据宽度动」的对偶模块，结构与本模块高度同构（按宽度关系分发到 up/down sizer 或直通），读完本讲再看它会非常轻松。
- 进入异构网络综合应用：[u15-l4 异构网络设计实战](u15-l4-heterogeneous-network.md) 会把 `axi_iw_converter` 与 `axi_cdc`、`axi_dw_converter`、`axi_xbar`、`axi_isolate` 一起用来拼一个跨时钟域、跨宽度、跨 ID 的真实片上网络，那是本讲「统一入口」哲学的最终舞台。
- 若想更牢地掌握 ID 子模块内部机理，可回头精读 [u10-l1 axi_id_remap](u10-l1-axi-id-remap.md) 的重映射表 FSM 与 [u10-l2 axi_id_serialize](u10-l2-axi-id-serialize.md) 的分桶+序列化通路——本讲的 `gen_remap`/`gen_serialize` 两个分支正是直接透传参数给它们。
