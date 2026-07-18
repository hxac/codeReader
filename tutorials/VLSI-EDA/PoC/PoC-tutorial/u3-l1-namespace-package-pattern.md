# 命名空间包模式

## 1. 本讲目标

PoC 仓库里 `src/` 下有十几个命名空间（arith、fifo、mem、io、net、cache……），每个命名空间又含若干个 IP 核。如果每写一个核都把自己的对外接口「散落」在自己文件里，使用者就要满仓库去翻每个核的 `entity`。PoC 的解决办法是：**每个命名空间都有一个「根包」`<ns>.pkg.vhdl`，集中声明该命名空间对外提供的组件、类型与函数**。

学完本讲，你应当能够：

1. 说清楚 `<ns>.pkg.vhdl` 这个文件在命名空间里扮演的「索引/清单」角色，以及它为什么必须先于各核被编译。
2. 看懂包里三种内容：`component` 组件声明、`type` 枚举/数据类型、`function` 辅助函数；并理解为什么有的包只含其中一两种。
3. 拿到一个陌生的 `<ns>.pkg.vhdl`，能快速浏览出「这个命名空间提供哪些核、有哪些配置开关、有哪些可复用函数」。
4. 写出「`.files` 编译清单 → pkg 声明 → `use PoC.<ns>.all` 消费 → 实例化」这条完整链路。

本讲只讲**命名空间包的组织模式**，不深入任何具体核的内部实现（FIFO 的握手、RAM 的读写时序等留待 u3-l3、u3-l4 等后续讲义）。

## 2. 前置知识

本讲假定你已经掌握 u1、u2 单元的基础，特别是：

- **VHDL 的 package 机制**：VHDL 用 `package` 把类型、常量、函数、组件声明集中放在一起，别处用一句 `use <库>.<包>.all;` 就能全部拿到。这是 VHDL 标准的代码复用手段，PoC 只是把它用到了极致。
- **PoC 的命名规则**（u1-l4）：源码实体一律写成 `<namespace>_<entity>` 的蛇形命名，例如宽加法器叫 `arith_addw`，属于 `arith` 命名空间；单实体单文件，文件名即 `<entity>.vhdl`。本讲要讲的 `<ns>.pkg.vhdl` 就是这个命名规则的「配套产物」。
- **`.files` 编译清单**（u2-l1）：pyIPCMI 用一种非 VHDL 的清单文件来规定「先编译谁、后编译谁、在什么条件下编译」。公共包 `src/common/` 必须最先编译，命名空间包紧随其后。
- **`PoC` 库**：所有 PoC 源码都编译进一个名为 `PoC` 的 VHDL 库，所以引用时写的是 `use PoC.<ns>.all;`，而不是 `work`。

一句话复习：**包是仓库，`use` 是借书证，`.files` 是上架顺序。** 本讲就是把这三者串起来。

## 3. 本讲源码地图

本讲围绕三个代表性命名空间包展开（外加一个子包和一个消费方作为佐证）：

| 文件 | 角色 | 本讲用途 |
| --- | --- | --- |
| `src/arith/arith.pkg.vhdl` | `PoC.arith` 命名空间根包 | **最完整的样例**：同时含 component + type + function |
| `src/fifo/fifo.pkg.vhdl` | `PoC.fifo` 命名空间根包 | **纯组件清单样例**：只含 component，是实践任务的对象 |
| `src/mem/mem.pkg.vhdl` | `PoC.mem` 命名空间根包 | **纯类型/函数样例**：演示组件被下放到子包 |
| `src/mem/ocram/ocram.pkg.vhdl` | `PoC.mem.ocram` 子包 | 演示「根包 + 子包」的二级拆分 |
| `src/arith/arith_addw.vhdl` | `arith_addw` 核 | 消费方样例：用 `use PoC.arith.all` 拿到枚举类型 |
| `src/fifo/fifo_cc_got.vhdl` | `fifo_cc_got` 核 | 消费方样例：跨命名空间实例化 `ocram_sdp` |
| `src/arith/arith_addw.files` | `.files` 编译清单 | 证明 pkg 先于核被编译 |

> 小贴士：`arith.pkg` 是三个里信息量最大、最适合当「教科书」读的一个；`fifo.pkg` 是最规整的「组件货架」；`mem.pkg` 则提醒你**真实项目里包的形态会因地制宜**，不要死记「每个包都必须有三样东西」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 命名空间包结构**、**4.2 组件声明**、**4.3 类型与函数**。

### 4.1 命名空间包结构

#### 4.1.1 概念说明

PoC 里每个命名空间（如 `arith`、`fifo`、`mem`）都有一个「根包」，文件名固定为 `<ns>.pkg.vhdl`，里面声明的 VHDL 包名就是 `<ns>`。你可以把它理解成**这个命名空间的「目录页 / API 清单」**：

- 它**集中罗列**本命名空间对外提供哪些核（`component`）、哪些配置用的枚举类型（`type`）、哪些可复用函数（`function`）。
- 它**先于**本命名空间里的任何核被编译，这样所有核都能用 `use PoC.<ns>.all;` 把清单里的东西一次性「借」到手。
- 它本身**不含可综合的电路逻辑**——`component` 只是接口声明，`function` 的实现放在 `package body` 里。

为什么需要这样的清单？因为 PoC 的命名规则是 `<namespace>_<entity>`（见 [vhdl_coding.md:10-12](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/vhdl_coding.md#L10-L12)），文件名自带「地址」：看到 `arith_addw`，你就知道它属于 `arith`，于是去 `arith.pkg.vhdl` 找它的对外声明。**包文件名 + 实体命名规则两者配合，让你不用翻文档就能定位任何一个核。**

#### 4.1.2 核心流程

命名空间包在整个工程里的「生命周期」如下：

```
  .files 编译清单                    VHDL 编译                 使用方
  ─────────────────                  ──────────                ──────
  vhdl PoC "src/arith/arith.pkg.vhdl"  ──▶ 先编译进 PoC 库       │
        （排在最前）                     得到「PoC.arith」包       │
                                                                    │
  vhdl PoC "src/arith/arith_addw.vhdl" ──▶ 后编译该核            │
        （排在 pkg 之后）                                         ▼
                                              使用方写：use PoC.arith.all;
                                              （拿到 component / type / function）
```

要点有三：

1. **顺序**：`.files` 里 pkg 永远排在同名命名空间的核**之前**。
2. **依赖**：pkg 顶部会 `use PoC.utils.all;`（甚至 `config`、`strings` 等），所以它自身又依赖 `src/common/` 先编译——这就是为什么每个命名空间的 `.files` 都会先 `include "src/common/common.files"`。
3. **消费**：核或测试台写一句 `use PoC.<ns>.all;` 就拿到全部对外内容，不必逐个 `use` 每个核。

#### 4.1.3 源码精读

先看 `arith.pkg.vhdl` 的开头，它是一个典型命名空间包的骨架：[src/arith/arith.pkg.vhdl:34-42](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl#L34-L42)。

```vhdl
library IEEE;
use     IEEE.std_logic_1164.all;
use     IEEE.numeric_std.all;

library PoC;
use     PoC.utils.all;          -- 依赖公共包 utils（log2ceil、T_BCD_VECTOR 等）

package arith is                 -- 包名 = 命名空间名 = arith
   ...                          -- 下面是 component / type / function
```

这段说明三件事：① 它 `use PoC.utils.all`，所以 `common` 必须先编译；② 包名 `arith` 与命名空间同名；③ 后面所有内容都装进这个 `package arith is ... end package;` 的「盒子」里。

再看编译顺序的证据——[src/arith/arith_addw.files:12-17](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.files#L12-L17)：

```
include   "src/common/common.files"        # 先加载公共包
vhdl  PoC "src/arith/arith.pkg.vhdl"       # 再编译 PoC.arith 包
vhdl  PoC "src/arith/arith_addw.vhdl"      # 最后才编译核本身
```

可以清楚看到：**common → pkg → 核**，pkg 夹在中间，既依赖 common，又被核依赖。这就是命名空间包在整个依赖图里的位置。

#### 4.1.4 代码实践

**实践目标**：亲手确认「pkg 先编译、核后编译」的顺序，并验证 `package` 名与命名空间同名。

**操作步骤**：

1. 打开 `src/arith/arith.pkg.vhdl`，找到 `package arith is` 这一行（约第 42 行）。
2. 打开 `src/arith/arith_addw.files`，对照第 12–17 行的注释顺序。
3. 用同样方法打开 `src/fifo/fifo_cc_got.files`，看它是否也遵循「先 common、再 pkg、最后核」的顺序。

**需要观察的现象**：每个命名空间的 `.files` 都先 `include` 公共包，再编译自己的 `<ns>.pkg.vhdl`，最后才编译具体核。

**预期结果**：你能指出「pkg 是命名空间里第一个被编译的本地文件」。如果某天你新增一个核却忘了把 pkg 加进 `.files`，该核会因为 `use PoC.<ns>.all` 找不到包而编译失败——这恰好反证了 pkg 的「地基」地位。

> 本结论可直接对照源码验证，无需运行仿真。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `arith.pkg.vhdl` 顶部要写 `use PoC.utils.all;`，而 `ocram.pkg.vhdl`（子包）顶部却没有写任何 `use PoC.*`？（提示：看它们各自声明里用到了哪些公共类型。）

> **答案**：`arith.pkg` 里的 `arith_counter_bcd` 用到了 `T_BCD_VECTOR`、`arith_firstone` 的端口用到了 `log2ceil(...)`，这两者都来自 `utils`，所以必须 `use PoC.utils.all`。`ocram.pkg` 里只用 `std_logic`/`std_logic_vector`/`unsigned` 这些 IEEE 标准类型，不依赖 PoC 公共包，所以顶部只 `use IEEE.*`。**包顶部的 `use` 列表，正好暴露了它依赖哪些公共设施。**

**练习 2**：如果把 `arith_addw.vhdl` 在 `.files` 里的顺序挪到 `arith.pkg.vhdl` 之前，会发生什么？

> **答案**：编译 `arith_addw.vhdl` 时会报错，因为它 `use PoC.arith.all;`（见 [src/arith/arith_addw.vhdl:51](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L51)）要求 `PoC.arith` 包已经存在；而包还没编译，于是 `use` 找不到目标。**顺序错了就编译不过，这是 pkg 必须先编译的直接体现。**

---

### 4.2 组件声明

#### 4.2.1 概念说明

`component` 是 VHDL 里对一个 `entity` 的**接口声明**（有哪些 generic、有哪些 port、各自的方向与类型）。它和 `entity` 的关系类似「函数原型 vs 函数定义」：`entity` 是真正的实现，`component` 是对外公布的「签名」。

把 `component` 集中放进命名空间包有两个直接好处：

1. **一处声明、处处复用**：使用者写 `use PoC.<ns>.all;` 后就能用命名绑定（`generic map` / `port map`）实例化该组件，**不必在每个调用方文件里重复抄一遍 component 声明**。
2. **接口即文档**：浏览一个包的 component 列表，就等于浏览这个命名空间的「货架」。PoC 的编码规范也明确鼓励这种做法——见 [vhdl_coding.md:85-87](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/vhdl_coding.md#L85-L87)：*「不要用位置绑定；如果组件声明已通过专用包提供，优先用组件方式实例化。」*

> 注意：PoC 的核大多同时支持两种实例化写法——**直接实体实例化** `entity PoC.fifo_cc_got` 和 **组件实例化** `fifo_cc_got`（需先 `use` 包）。前者不强制依赖包，后者依赖包里的 component 声明。两种都合法，但 component 声明让接口更显式、更易被工具与文档消费。

#### 4.2.2 核心流程

一个 component 声明与它的实体之间的对应关系：

```
   component 声明（在 <ns>.pkg.vhdl 里）          entity 实现（在 <entity>.vhdl 里）
   ──────────────────────────────────           ──────────────────────────────
   component fifo_cc_got                         entity fifo_cc_got is
     generic ( D_BITS  : positive;                generic ( D_BITS  : positive;
               MIN_DEPTH: positive; ... );                 MIN_DEPTH: positive; ... );
     port    ( put : in  std_logic;               port    ( put : in  std_logic;
               ... );                                       ... );
   end component;                                end entity;
                  │                                              │
                  └──────────── 接口应保持一致 ────────────────────┘
```

实例化时（以 [src/fifo/fifo_cc_got.vhdl:317-321](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L317-L321) 为真实例子）：

```vhdl
ram : entity PoC.ocram_sdp        -- 直接实体实例化，命名绑定
  generic map (
    A_BITS => A_BITS,
    D_BITS => D_BITS
  )
  port map ( ... );
```

这里 `fifo_cc_got` 属于 `fifo` 命名空间，却实例化了 `mem` 命名空间下的 `ocram_sdp`——靠的就是先 `use poc.ocram.all;`（[src/fifo/fifo_cc_got.vhdl:95](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L95)）把 `ocram` 包里的 component 声明「借」过来。**命名空间包让跨命名空间复用变得和写一句 `use` 一样简单。**

#### 4.2.3 源码精读

`fifo.pkg.vhdl` 是一份最规整的「纯组件货架」——它**只含 component，没有 type 也没有 function**，因此连 `package body` 都没有，直接以 `end package;` 收尾（[src/fifo/fifo.pkg.vhdl:43](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo.pkg.vhdl#L43) 起，至 [src/fifo/fifo.pkg.vhdl:290](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo.pkg.vhdl#L290) 止）。

挑两个有代表性的 component 看。最小的一个是 `fifo_glue`，[src/fifo/fifo.pkg.vhdl:46-65](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo.pkg.vhdl#L46-L65)：

```vhdl
-- Minimal FIFO with single clock to decouple enable domains.
component fifo_glue
  generic ( D_BITS : positive );               -- 数据位宽
  port (
    clk : in std_logic;  rst : in std_logic;   -- 单时钟 + 同步复位
    put : in  std_logic;  di : in  ...;  ful : out std_logic;   -- 写侧
    vld : out std_logic;  do : out ...;  got : in  std_logic    -- 读侧
  );
end component;
```

功能最全的一个是 `fifo_cc_got`，[src/fifo/fifo.pkg.vhdl:120-146](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo.pkg.vhdl#L120-L146)，它带了一长串配置 generic（`DATA_REG`、`STATE_REG`、`OUTPUT_REG`、`ESTATE_WR_BITS`、`FSTATE_RD_BITS`），并暴露 `estate_wr` / `fstate_rd` 这些填充指示器端口。两个 component 的注释行（`-- Minimal FIFO ...`、`-- Full-fledged FIFO ...`）就是该核的「一句话说明书」，这也是 PoC 包文件好读的原因。

再看 `mem` 命名空间的一个**反例**：`mem.pkg.vhdl` 里**一个 component 都没有**（[src/mem/mem.pkg.vhdl:61-83](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/mem.pkg.vhdl#L61-L83) 全是 type 与 function）。`mem` 下的存储器组件其实被放进了子包 `ocram.pkg.vhdl`，例如单口 RAM 组件 `ocram_sp` 见 [src/mem/ocram/ocram.pkg.vhdl:42-55](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram.pkg.vhdl#L42-L55)，简单双口 `ocram_sdp` 见 [src/mem/ocram/ocram.pkg.vhdl:58-74](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/ocram/ocram.pkg.vhdl#L58-L74)。这说明：**命名空间包是「自然聚集点」，但内容形态因命名空间而异**——`arith` 把组件、类型、函数全塞进根包；`fifo` 把组件全塞进根包；`mem` 则把组件按家族（ocram/ocrom）拆进各自的子包，根包只留跨家族共享的类型与函数。

> ⚠️ 注意：`src/mem/README.md` 第 7–8 行写「`PoC.mem` 持有本命名空间全部组件声明」，但实际源码里组件声明在 `ocram.pkg` / `ocrom.pkg` 子包中。**以源码为准**：文档描述的是「理想模式」，`mem` 是把该模式细化成了「共享根包 + 每家族子包」。

#### 4.2.4 代码实践（本讲核心实践任务）

**实践目标**：通读 `fifo.pkg.vhdl`，列出它声明的**全部** FIFO 组件名，并依据命名约定与文件内注释标注每个组件的使用场景。

**操作步骤**：

1. 打开 [src/fifo/fifo.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo.pkg.vhdl)，从第 43 行 `package fifo is` 一路读到第 290 行 `end package;`。
2. 把每一段 `component <名字>` 的名字抄下来，并把它正上方那行 `-- ...` 注释也抄下来。
3. 对照下面的「命名约定钥匙」给每个组件标注使用场景。

**FIFO 命名约定钥匙**（`<前缀>` + `<功能>` + `<读侧>` 的组合）：

| 命名片段 | 含义 | 中文场景 |
| --- | --- | --- |
| `cc` | common clock | 单时钟域（同钟） |
| `ic` | independent clock | 独立时钟域（跨钟） |
| `dc` | dual clock | 双时钟（跨钟的另一种叫法） |
| `glue` | 最小粘合 | 解耦使能域的最小缓冲 |
| `ll` | Local-Link | Local-Link 协议接口 |
| `shift` | shift register | 用移位寄存器当后端（小深度） |
| `got` | got 选通 | 读侧用 `got`（读完成）选通 |
| `tempput` / `tempgot` | 投机写 / 投机读 | 支持 commit / rollback 回滚 |
| `assembly` | 装配缓冲 | 按地址范围写入、带 generation 守卫 |

**预期结果（参考答案表）**：

| 组件名 | 行号 | 使用场景（依据注释 + 命名） |
| --- | --- | --- |
| `fifo_glue` | L46–65 | 最小 FIFO，单时钟，解耦使能域 |
| `fifo_ll_glue` | L68–94 | 最小 Local-Link FIFO，单时钟，FWFT（首字直通）模式 |
| `fifo_shift` | L97–117 | 简单 FIFO，移位寄存器后端（适合小深度） |
| `fifo_cc_got` | L120–146 | 单时钟（cc=同钟）全功能 FIFO，基于片上 RAM |
| `fifo_dc_got_sm` | L148–163 | 双时钟（dc=跨钟）小型 FIFO |
| `fifo_ic_got` | L165–191 | 独立时钟（ic=跨钟）全功能 FIFO |
| `fifo_cc_got_tempput` | L193–222 | 单时钟，写侧支持投机写 + commit/rollback |
| `fifo_cc_got_tempgot` | L224–253 | 单时钟，读侧支持投机读 + commit/rollback |
| `fifo_ic_assembly` | L255–288 | 跨钟，按地址装配的缓冲（带 generation 守卫） |

> 共 **9 个** component。如果你只数到 8 个，多半是漏掉了注释里没明显标识场景的 `fifo_ll_glue` 或最末尾的 `fifo_ic_assembly`。**注意 `fifo.pkg.vhdl` 里没有任何 `type` 和 `function`，也没有 `package body`**——它是「纯组件货架」的典型。

#### 4.2.5 小练习与答案

**练习 1**：`fifo_cc_got` 与 `fifo_ic_got` 的 generic/port 几乎一样，主要差别在哪里？这个差别对应什么样的应用场景？

> **答案**：差别在时钟与复位——`fifo_cc_got` 只有一对 `rst, clk`（[L132](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo.pkg.vhdl#L132)），读写共享同一时钟；`fifo_ic_got` 把读写拆成 `clk_wr/rst_wr` 与 `clk_rd/rst_rd` 两组（[L176-L185](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo.pkg.vhdl#L176-L185)）。前者用于同一时钟域内的数据缓冲（同钟），后者用于两个异步时钟域之间的数据传递（跨钟，需要内部做 CDC）。

**练习 2**：为什么 `fifo.pkg.vhdl` 没有 `package body`，而 `arith.pkg.vhdl` 有？

> **答案**：`package body` 只在包里声明了 `function`/`procedure`/延迟常量时才需要（函数实现写在 body 里）。`fifo.pkg` 全是 `component` 声明，component 不需要 body；`arith.pkg` 声明了函数 `arith_div_latency`，所以必须有一个 body 来写它的实现（见 [src/arith/arith.pkg.vhdl:238-243](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl#L238-L243)）。**有没有 body，是判断一个包是否含函数的快捷信号。**

---

### 4.3 类型与函数

#### 4.3.1 概念说明

除了组件，命名空间包还常装两样东西：

- **`type`（尤其是枚举类型）**：把一个核的「配置选项」做成具名取值。比如宽加法器有多种「架构方案」，与其用整数 `0/1/2/3` 表示，不如定义枚举 `tArch is (AAM, CAI, CCA, PAI)`，generic 里直接写 `ARCH : tArch := AAM`，既自文档化又防写错。
- **`function`**：本命名空间里反复用到的小工具，比如除法器核需要算「流水线延迟等于多少拍」，就把它写成一个函数 `arith_div_latency`，放进包里，所有相关核与测试台都能调。

这两者的价值在于**「全命名空间共享」**：类型与函数在根包里声明一次，所有核、子包、测试台通过 `use PoC.<ns>.all;` 共享同一份定义，避免各核各写一套、口径不一。

#### 4.3.2 核心流程

```
   ┌──────────────── <ns>.pkg.vhdl ────────────────┐
   │                                                │
   │  type tXxx is ( ... );          ← 枚举「配置开关」│
   │  function f(...) return ...;    ← 函数「原型」  │
   │  ......                                        │
   │  end package;                                  │
   │                                                │
   │  package body <ns> is                          │
   │    function f(...) return ... is ... end;      │← 函数「实现」
   │  end package body;                             │
   └────────────────────────────────────────────────┘
                         │ use PoC.<ns>.all;
                         ▼
        核 entity 的 generic 用上 tXxx；测试台调上 f(...)
```

要点：**函数原型进 `package`、实现进 `package body`**；枚举类型只需在 `package` 里声明一次，无需 body。

#### 4.3.3 源码精读

`arith.pkg.vhdl` 同时给出了类型与函数两个范例。枚举类型见 [src/arith/arith.pkg.vhdl:161-163](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl#L161-L163)：

```vhdl
type tArch     is (AAM, CAI, CCA, PAI);   -- 宽加法器的架构方案
type tBlocking is (DFLT, FIX, ASC, DESC); -- 分块方案
type tSkipping is (PLAIN, CCC, PPN_KS, PPN_BK); -- 进位跳越方案
```

这三个枚举是给 `arith_addw` 当 generic 用的「配置旋钮」。函数声明见 [src/arith/arith.pkg.vhdl:81-85](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl#L81-L85)（含一段解释性注释），实现则在 body 里 [src/arith/arith.pkg.vhdl:239-242](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl#L239-L242)：

```vhdl
function arith_div_latency(a_bits, rapow : positive) return positive;   -- 原型
...
function arith_div_latency(a_bits, rapow : positive) return positive is -- 实现
begin
  return (a_bits+rapow-1)/rapow;   -- 向上取整地算出流水线拍数
end;
```

> 这个函数体里的 `(a_bits+rapow-1)/rapow` 正是 u2-l2 讲过的「整数向上取整除法」`div_ceil` 的等价写法，可见命名空间包里的函数也复用着公共包 `utils` 的思路。

**消费方样例**：`arith_addw` 这个核在自己的 entity 里直接用上了包里的枚举类型，[src/arith/arith_addw.vhdl:49-63](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L49-L63)：

```vhdl
library PoC;
use   PoC.utils.all;
use   PoC.arith.all;          -- 拿到 tArch / tBlocking / tSkipping

entity arith_addw is
  generic (
    N         : positive;
    K         : positive;
    ARCH      : tArch     := AAM;     -- 直接用包里的枚举类型与字面量 AAM
    BLOCKING  : tBlocking := DFLT;
    SKIPPING  : tSkipping := CCC;
    ...
```

这正是「类型在包里集中声明、在核里复用」的标准用法。注意：这里 `use PoC.arith.all` 其实是为了**那三个枚举类型**，不是为了组件（`arith_addw` 自身就是被声明的核之一）。

**纯类型/函数样例**：`mem.pkg.vhdl` 是另一个极端——它**只有类型和函数，没有组件**。[src/mem/mem.pkg.vhdl:62-82](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/mem/mem.pkg.vhdl#L62-L82)：

```vhdl
type T_MEM_FILEFORMAT is (MEM_FILEFORMAT_INTEL_HEX, MEM_FILEFORMAT_LATTICE_MEM, MEM_FILEFORMAT_XILINX_MEM);
type T_MEM_CONTENT    is (MEM_CONTENT_BINARY, MEM_CONTENT_DECIMAL, MEM_CONTENT_HEX);

function  mem_FileExtension(Filename : string) return string;
impure function mem_ReadMemoryFile(FileName : string; ...; FORMAT : T_MEM_FILEFORMAT; ...)
            return T_SLM;
```

这两个枚举（文件格式、内容进制）和两个函数（取扩展名、读内存初始化文件）是 `mem` 下所有存储器核（`ocram_sp`、`ocrom_sp` 等）**共享的**辅助能力：任何核要从一个 `.mif`/`.mem` 文件加载初值，都会调 `mem_ReadMemoryFile`。所以它们放在 `mem` 根包里，供 `ocram`、`ocrom` 两个子包共用——这正是把根包当作「跨子包共享层」的设计。

#### 4.3.4 代码实践

**实践目标**：追踪一个枚举类型「从包声明到核消费」的完整路径，验证类型确实是被共享的。

**操作步骤**：

1. 在 [src/arith/arith.pkg.vhdl:161](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith.pkg.vhdl#L161) 找到 `type tArch is (AAM, CAI, CCA, PAI);`。
2. 在 [src/arith/arith_addw.vhdl:51](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L51) 看到 `use PoC.arith.all;`。
3. 在 [src/arith/arith_addw.vhdl:59](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/arith/arith_addw.vhdl#L59) 看到 `ARCH : tArch := AAM;`。
4. 用编辑器/`grep` 在 `src/arith/` 下搜索 `tArch`，看还有哪些文件用到它。

**需要观察的现象**：`tArch` 这个类型只在 `arith.pkg.vhdl` 里被**定义**一次，却在多个核文件里被**使用**（`arith_addw` 至少是其一）。

**预期结果**：你会看到「定义点 1 处 + 使用点多处」的格局，这就是「集中声明、分散复用」。若想进一步验证，可在 `src/mem/ocram/` 下搜 `FILENAME`，看 `ocram.pkg` 里的 `FILENAME : string := ""` 这个 generic 又被哪些 ocram 核消费——同理。

> 本实践为源码阅读型，无需运行；如要本地验证「多处使用」，可用 `grep -rn "tArch" src/arith/`。

#### 4.3.5 小练习与答案

**练习 1**：`arith_div_latency` 的原型写在 `package arith` 里、实现写在 `package body arith` 里。如果有人把实现也写进 `package arith`（spec）里会怎样？

> **答案**：VHDL 规定 `package`（规格）里只能放函数**原型**（`function ... return ...;` 末尾分号、无 `is ... return ...`），实现必须放 `package body`。把实现写进 spec 会直接语法报错。**记住口诀：「声明在 spec，实现在 body」。**

**练习 2**：`mem.pkg.vhdl` 里 `mem_ReadMemoryFile` 标了 `impure`，而 `arith_div_latency` 没标。为什么？

> **答案**：`impure` 表示函数可能有副作用或依赖外部状态、两次调用同样参数可能返回不同结果。`mem_ReadMemoryFile` 要**读磁盘文件**（依赖文件内容与读取位置），结果不可纯函数化，所以必须 `impure`；`arith_div_latency` 是纯数学公式 `(a_bits+rapow-1)/rapow`，给定输入永远同一输出，所以是默认的 `pure`。**看到 `impure`，就联想到「它碰了文件/共享变量/随机数」。**

---

## 5. 综合实践

设计一个把三个最小模块串起来的小任务：**画出 `fifo_cc_got` 这个核「从编译清单到实例化」的完整链路图，并解释其中涉及的命名空间包。**

**任务要求**：

1. **编译侧**：打开 [src/fifo/fifo_cc_got.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.files)，按顺序写出它依赖了哪些 `.files` 与源文件（提示：第 8 行 include 公共包，第 14 行 include 了 `src/mem/ocram/ocram_sdp.files`，第 17 行才是 `fifo_cc_got.vhdl` 本身）。
2. **声明侧**：指出 `fifo_cc_got` 这个组件的对外声明在哪个包（答：`PoC.fifo`，见 fifo.pkg.vhdl L120–146），而它在核内部实例化用到的 `ocram_sdp` 又声明在哪个包（答：`PoC.ocram` 子包，见 ocram.pkg.vhdl L58–74）。
3. **消费侧**：在 [src/fifo/fifo_cc_got.vhdl:95](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L95) 找到 `use poc.ocram.all;`，在 [src/fifo/fifo_cc_got.vhdl:317](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/fifo/fifo_cc_got.vhdl#L317) 找到 `ram : entity PoC.ocram_sdp` 的实例化。
4. **画图**：用一张文字流程图把上面三侧串起来，形如：

```
src/common/common.files ─┐
src/mem/ocram/ocram_sdp.files ─▶ ocram.pkg.vhdl(PoC.ocram) ─▶ ocram_sdp.vhdl
                                                                │
                              fifo_cc_got.files ───────────────▶│
                                  │                             ▼
                                  └─▶ fifo_cc_got.vhdl ── use poc.ocram.all;
                                                       └─ ram : entity PoC.ocram_sdp  (实例化)
```

**预期结果**：你能用一句话说清——*`fifo_cc_got`（fifo 命名空间）通过 `use poc.ocram.all` 复用了 `mem` 命名空间下 `ocram` 子包声明的 `ocram_sdp` 组件，而这一切能编译通过，是因为 `.files` 把公共包、ocram 子包、fifo 核按正确顺序串了起来。* 这条链路同时验证了本讲的三个模块：包结构（编译顺序）、组件声明（跨命名空间复用）、类型与函数（公共设施共享）。

> 若要本地验证顺序，可对照源码逐行核对；本任务为源码阅读型，无需运行仿真。

## 6. 本讲小结

- **命名空间包是清单**：每个命名空间有一个 `<ns>.pkg.vhdl`，集中声明该命名空间对外提供的 component / type / function，相当于该命名空间的「API 目录页」。
- **它必须先编译**：`.files` 里公共包 → 命名空间 pkg → 具体核，顺序错了核会因为 `use PoC.<ns>.all` 找不到包而编译失败。
- **组件声明即接口文档**：把 component 集中放包里，使用者一句 `use` 就能命名绑定实例化；PoC 同时支持直接实体实例化与组件实例化两种写法。
- **类型与函数实现全命名空间共享**：枚举类型（如 `tArch`）当 generic 配置旋钮，函数（如 `arith_div_latency`）封装共用算法；声明在 `package`、实现在 `package body`。
- **包的形态因地制宜**：`arith.pkg` 三样俱全，`fifo.pkg` 纯组件无 body，`mem.pkg` 纯类型/函数且把组件下放到 `ocram`/`ocrom` 子包——不要死记「每个包必须三样都有」。
- **跨命名空间复用靠 `use`**：`fifo_cc_got` 写 `use poc.ocram.all` 就能实例化 `mem` 下的 `ocram_sdp`，命名空间包让这种复用变成一行代码。

## 7. 下一步学习建议

本讲只讲了命名空间包的**组织模式**，没有进入任何具体核的内部实现。建议按下面的顺序继续：

1. **u3-l2 厂商选择与可移植机制**：看一个核（`sync_Bits`）如何依据 `DEVICE_INFO.Vendor` 在通用实现与厂商专用子实体间选择——届时你会再次体会到「通用包装实体 + 厂商专用子实体」的分层，与本讲的「根包 + 子包」是同一种思路。
2. **u3-l3 片上 RAM 抽象：ocram 家族**：本讲反复出现的 `ocram.pkg` / `ocram_sp` / `ocram_sdp` 会在那里被完整拆解，包括它如何对接厂商原语。
3. **u3-l4 FIFO 家族**：本讲 4.2 列出的 9 个 FIFO 组件的内部实现、`put/got/valid/full` 握手与 `estate_wr/fstate_rd` 填充指示器，会在那里深入。
4. **想马上练手**：挑一个本讲没细看的命名空间包（如 `src/io/io.pkg.vhdl` 或 `src/net/net.pkg.vhdl`），用本讲的方法论自己画一张「它有哪些 component、有没有 type/function、有没有子包」的清单，检验你是否真的掌握了命名空间包模式。
