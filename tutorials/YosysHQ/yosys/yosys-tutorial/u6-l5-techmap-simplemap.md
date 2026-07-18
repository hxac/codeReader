# techmap 与 simplemap：工艺映射基础

## 1. 本讲目标

经过前几讲，我们已经能让一段 Verilog 走完 `proc → opt → memory`，得到一个由 `$and`、`$or`、`$mux`、`$dff`、`$mul`、`$add` 这类**高层内部单元**组成的网表。但这些单元仍然是「参数化的、多位宽的抽象运算」，离真正的「门」还有一步之遥。本讲就来走完这最后一步——**工艺映射（technology mapping）**。

学完本讲，你应当能够：

1. 说清楚 `techmap` 的核心思想：它是一个**不懂任何 RTLIL 语义、纯靠「模板模块」做模式匹配替换**的通用映射器。
2. 区分 `techmap.v`、`simlib.v`、`simcells.v` 三个长得相似的 `.v` 文件的职责——它们分别是「实现库」「高层仿真模型」「门级仿真模型」。
3. 理解 `simplemap` 是一份**写死在 C++ 里的、只能处理一批「机械」单元**的快速映射器，并能解释它为什么**无法**处理 `$mul`/`$add` 这类算术单元。
4. 动手对含 `$mul`/`$add` 的设计执行 `techmap`，观察高层算术单元如何被层层替换为 `$_AND_`/`$_OR_`/`$_MUX_` 等门级原语，并与 `simplemap` 的输出做对比。

## 2. 前置知识

本讲需要以下已建立的概念（来自前置讲义）：

- **两层内部单元库**（u3-l4）：Yosys 的单元分两层。一层是**参数化高层单元**（`$and`、`$or`、`$mux`、`$dff`、`$mul`、`$add`……以 `$` 开头，位宽可变，综合早中期的目标）；另一层是**单位宽门级原语**（`$_AND_`、`$_OR_`、`$_NOT_`、`$_MUX_`、`$_DFF_P_`……以 `$_` 开头，把极性/边沿编进名字里，工艺映射后期的目标）。本讲就是连接这两层的桥。
- **RTLIL 构造接口**（u3-l1）：`module->addCell()`、`cell->setPort()`、`module->connect()`、`NEW_ID`，这些是理解 `simplemap` 如何在 C++ 里直接造门的钥匙。
- **综合主流程的位置**（u6-l2/u6-l3/u6-l4）：`proc` 把 `always` 翻成 `$mux`/`$dff`，`opt` 收拾网表，`memory` 把 `$mem` 拆成 `$dff`+`$mux`。本讲的 `techmap`/`simplemap` 紧随其后，把残留的高层 `$` 单元下沉为门。
- **ScriptPass 的双模式**（u4-l2）：`synth` 的 fine 阶段会调用 `techmap`，coarse 阶段会先调用 `alumacc`。

一个直觉性的比喻：把高层 `$` 单元想象成「函数调用」，把门级 `$_` 原语想象成「机器指令」。`techmap`/`simplemap` 就是把函数调用**内联展开**成指令序列的过程——而且这个「展开规则」本身是用普通的 Verilog/RTLIL 模块写出来的，这就是它最巧妙的地方。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `passes/techmap/techmap.cc` | `techmap` 命令的全部实现：加载映射库、匹配模板、内联替换。本讲的主角。 |
| `passes/techmap/simplemap.cc` | `simplemap` 命令的实现，以及一批把 `$` 单元**逐位拆成** `$_` 门的 C++ 函数。 |
| `passes/techmap/simplemap.h` | `simplemap` 对外暴露的函数接口，供 `techmap` 复用。 |
| `techlibs/common/techmap.v` | **内置映射模板库**（实现库）。用 Verilog 写出每个 `$` 单元如何由 `$_` 门或更小的 `$` 单元实现。 |
| `techlibs/common/simlib.v` | **高层 `$` 单元的行为级仿真模型**，用于仿真验证，不是映射库。 |
| `techlibs/common/simcells.v` | **门级 `$_` 单元的行为级仿真模型**，是 techmap/simplemap 的产出目标，也是 `abc` 的输入。 |
| `techlibs/common/synth.cc` | 通用综合脚本，展示 `techmap` 在 fine 阶段、`alumacc` 在 coarse 阶段的位置。 |

## 4. 核心概念与源码讲解

### 4.1 techmap 映射原理：用「模板模块」替换单元

#### 4.1.1 概念说明

「工艺映射」的目标是：把抽象的高层 `$` 单元，替换成更接近真实硬件的底层单元。比如把一个 8 位的 `$and` 换成 8 个 1 位的 `$_AND_` 门。

绝大多数综合工具的工艺映射都是**写死在代码里**的——遇到 `$and` 就生成 `$_AND_`。但 Yosys 的 `techmap` 走了一条更优雅的路：**它本身对 RTLIL 一无所知**。它只是一台「模板替换机器」：

- 你给它一个**映射库（map library）**，里面是一堆普通的 Verilog/RTLIL 模块，每个模块声明「我能用来替换哪种 `$` 单元，以及怎么替换」。
- `techmap` 扫描设计里的每个 cell，按 cell 的 `type` 去库里找匹配的模板模块。
- 找到后，它把模板模块的**内部内容（线、子单元、连接）原样内联（inline）**进当前模块，替换掉原来的 cell，并接好端口。

正因为「映射规则 = 普通模块」，所以**新增一种单元的映射，不需要改任何 C++ 代码**，只要往 `.v` 文件里加一个模块即可。这和 u4-l1 讲过的「pass 去中心化注册」是同一种设计哲学。

一个推论：`flatten`（u6-l1）本质上就是「用设计自身当映射库的 techmap」——把子模块实例内联进父模块。`techmap` 的 help 文本也明确点出了这一点。

#### 4.1.2 核心流程

`techmap` 对每个模块反复执行下面的循环，直到不再变化（不动点）：

1. **建索引**：读入映射库（默认是 `+/techmap.v`），为每个模板模块建立「cell 类型 → 模板模块名列表」的映射表 `celltypeMap`（一个类型可能对应多个模板，按字典序逐个尝试）。
2. **拓扑排序**：对当前模块里待映射的 cell，按数据依赖（谁的输出喂给谁的输入）做拓扑排序，避免替换顺序混乱。
3. **逐个匹配**：对每个 cell，遍历它类型对应的候选模板：
   - 检查模板的「特殊属性」（`techmap_simplemap`/`techmap_maccmap`/`techmap_wrap`），若命中则走专用路径；
   - 否则把模板按当前 cell 的参数**派生（derive）**出一份位宽确定的具体副本；
   - 处理模板里的 `_TECHMAP_DO_*` 命令线、`_TECHMAP_FAIL_` 失败标记、`_TECHMAP_*` 特殊参数。
4. **内联替换**：调用 `techmap_module_worker` 把模板的内容（线、cell、连接）改名后塞进当前模块，接好端口，最后**删除原 cell**。
5. 若产生了新的可映射 cell（比如模板里又用了别的 `$` 单元），回到第 2 步继续。

用一个伪代码概括：

```
map_lib = load("+/techmap.v")              # 默认实现库
celltypeMap = { cell.type : [模板模块...] }  # 按类型建索引
repeat:
    did = false
    for cell in topo_sort(module.cells):
        for tpl in celltypeMap.get(cell.type):
            if match(cell, tpl):            # 参数/常量约束满足
                inline(module, cell, tpl)   # 内联模板内容
                module.remove(cell)         # 删除原 cell
                did = true
                break
until not did
```

#### 4.1.3 源码精读

**默认映射库的加载**：当用户没给 `-map` 时，`techmap` 自动加载内置库 `+/techmap.v`（`+/` 前缀由 `proc_share_dirname()` 解析为安装目录的 share 路径）。

[techmap.cc:1207-1222](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/techmap.cc#L1207-L1222) —— 不带 `-map` 就用 `"+/techmap.v"` 作为内置库读入一个临时 `Design`（即 `map`）。这是「数据驱动映射」的入口。

**建索引 `celltypeMap`**：

[techmap.cc:1226-1257](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/techmap.cc#L1226-L1257) —— 遍历映射库的每个模块：若模块带 `techmap_celltype` 属性，则按属性里（空格分隔、支持 `[PN]` 通配展开）列出的类型登记；否则用模块名本身当匹配类型。结果是一张「cell 类型 → 候选模板名集合」的表。

**主循环 + 拓扑排序**：

[techmap.cc:412-469](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/techmap.cc#L412-L469) —— `techmap_module` 先跳过 blackbox；然后对每个选中 cell，依据端口输入/输出关系（`cell_to_inbit`、`outbit_to_cell`）建立依赖边，交给 `TopoSort` 排序。这一步保证了「先替换上游 cell」。

**专用映射器判定**：

[techmap.cc:488-495](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/techmap.cc#L488-L495) —— 如果模板带 `techmap_simplemap`/`techmap_maccmap`/`techmap_wrap` 属性，就走 `extmapper`（外部映射器）分支，把活儿交给对应的 C++ 实现或另一个 pass。这是 `techmap` 与 `simplemap`/`maccmap`/`alumacc` 的衔接点。

**内联替换的核心**：

[techmap.cc:322-388](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/techmap.cc#L322-L388) —— `techmap_module_worker` 把模板里的每个子 cell 复制进当前模块，用 `apply_prefix` 给名字加前缀防止冲突，用 `setPort` 重接端口。`apply_prefix` 的实现见 [techmap.cc:42-48](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/techmap.cc#L42-L48)：`\` 开头的公有名前缀化成 `cell.名`、`$` 开头的内部名前缀化成 `$techmapcell.名`。

[techmap.cc:399](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/techmap.cc#L399) —— 一切接好后，`module->remove(cell);` 删掉被替换的原 cell。这正是「内联展开」的收尾。

**外层模块循环**：

[techmap.cc:1276-1293](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/techmap.cc#L1276-L1293) —— 对设计里的每个模块反复跑 `techmap_module` 直到 `did_something` 为假，受 `-max_iter` 上限约束。

#### 4.1.4 代码实践

**实践目标**：亲眼看到一个高层 `$` 单元被 `techmap` 替换成门级原语。

**操作步骤**：

1. 准备一个最简单的位运算设计 `bit.v`：

```verilog
// 示例代码
module bit(input [3:0] a, b, output [3:0] y);
    assign y = a & b;
endmodule
```

2. 在 `yosys` 交互 shell 里执行：

```
yosys> read_verilog bit.v
yosys> proc; opt_clean
yosys> write_rtlil before.il     # 此时应有 1 个 $and 单元
yosys> techmap
yosys> write_rtlil after.il      # 此时 $and 应被替换为若干 $_AND_
```

3. 用任意文本工具对比 `before.il` 与 `after.il`。

**需要观察的现象**：`before.il` 里是一个 4 位的 `$and` 单元（端口 `A`/`B`/`Y` 各 4 位）；`after.il` 里 `$and` 消失，变成 4 个 1 位的 `$_AND_` 门，每个门的 `A`/`B`/`Y` 都是单 bit。

**预期结果**：`stat` 命令在 `techmap` 前显示 `'$and'` cells: 1，之后显示 `'$_AND_'` cells: 4。具体位宽随你的设计而定。

> 若本地尚未构建 yosys，构建方式见 u1-l2；本实践的具体输出数值为「待本地验证」，但 `$and → $_AND_` 的替换关系是确定的。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `techmap` 需要对 cell 做拓扑排序后再替换？如果不排序会出什么问题？

> **答案**：因为替换一个 cell 会改变网表结构（删 cell、加新线和子 cell），若先替换了下游 cell，其上游信号可能在替换过程中处于不一致状态；按数据依赖拓扑排序能保证先展开上游，使每一步的端口连接都基于稳定的信号。

**练习 2**：`techmap` 的 help 里说 `flatten` 是「用设计自身当映射库的 techmap」。请结合本节原理，解释这句话。

> **答案**：`flatten` 把子模块实例内联进父模块，这与 `techmap` 把模板模块内联进当前模块是**完全相同的机制**——区别只在于「映射库」是谁：`techmap` 的库是 `techmap.v`，`flatten` 的库是设计自己。所以 `flatten` 复用了同一套内联逻辑。

---

### 4.2 映射模板文件：techmap.v / simlib.v / simcells.v 的分工

#### 4.2.1 概念说明

初学者最容易混淆 yosys 里的三个 `.v` 文件：`techmap.v`、`simlib.v`、`simcells.v`。它们都定义了 `$`/`$_` 单元，但**用途完全不同**，绝不能混为一谈。

| 文件 | 定义什么 | 用途 | 被谁用 |
|------|---------|------|--------|
| `techmap.v` | 每个 `$` 单元**如何由门实现** | **映射库**（实现） | `techmap` 命令读它来做替换 |
| `simlib.v` | 高层 `$` 单元的**行为模型** | **仿真**（验证网表功能对不对） | 仿真器/形式验证前端 |
| `simcells.v` | 门级 `$_` 单元的**行为模型** | **仿真**门级网表 | 仿真器，也是 `abc` 的输入约定 |

一句话区分：**`techmap.v` 是「怎么造」，`simlib.v`/`simcells.v` 是「怎么仿真」**。`techmap` 只读 `techmap.v`，从不读 sim 库；sim 库也从不参与映射。

`techmap.v` 里的映射规则是通过**模块属性**来声明和驱动的，最重要的几类属性：

- `techmap_celltype = "..."`：声明本模块能匹配哪些 cell 类型（空格分隔多个，支持 `$_DFF_[PN]_` 这类通配）。
- `techmap_simplemap`：声明「匹配后交给 `simplemap` 处理」（见 4.3）。
- `techmap_maccmap`：声明「交给 `maccmap` 处理」（用于 `$macc`）。
- `techmap_wrap = "命令"`：声明「为这个 cell 建一个包装模块，然后跑指定命令（如 `alumacc`）」。

此外，模板里还可以用**特殊线网**和**特殊参数**与 `techmap`「对话」：

- `_TECHMAP_DO_*`：值是一段 yosys 命令字符串，`techmap` 会在模板上**真的执行**这段命令（比如 `proc`、`opt`）。这是让「行为级模板」自动变成门级的关键。
- `_TECHMAP_FAIL_`：若它被求值为非零常数，`techmap` 就放弃这个模板、尝试下一个候选。
- `_TECHMAP_CELLTYPE_`、`_TECHMAP_CONSTMSK_<端口>_`、`_TECHMAP_CONNMAP_<端口>_` 等：把当前 cell 的类型、常量位掩码、连接关系作为参数注入模板，供模板做条件判断。

#### 4.2.2 核心流程

`techmap.v` 是如何用上面这些机制把每种 `$` 单元映射出去的，分三种典型模式：

1. **委托 simplemap**（最省事）：对一批「机械」的位/逻辑/比较/多路/寄存器单元，写一个**空模块**，只挂 `techmap_simplemap` + `techmap_celltype` 两个属性，让 `techmap` 转交给 C++ 的 `simplemap` 去逐位拆分。
2. **行为级模板 + `_TECHMAP_DO_`**（最灵活）：对移位、全加器这类不便硬编码的单元，用 `always`/`for` 写出行为级实现，再用 `_TECHMAP_DO_00_ = "proc;;"` 让 `techmap` 自动跑 `proc` 把行为级翻译成门，最后往往再跟一条 `opt`。
3. **委托别的 pass**（最强大）：对算术单元 `$mul/$add/$sub/$neg/$lt...`，挂 `techmap_wrap = "alumacc"`，让 `techmap` 建包装模块并调用 `alumacc` pass 把它们转成 `$macc`，再由 `$macc` 的 `techmap_maccmap` 规则交给 `maccmap` 落地。

`$mul` 的完整下沉链（见第 5 节综合实践）就是模式 3 + 模式 2 + 模式 1 的接力。

#### 4.2.3 源码精读

**`techmap.v` 头部自述**：

[techmap.v:18-31](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/techmap.v#L18-L31) —— 明确写了：本库把内部 cell（如多位 `$not`）映射到内部逻辑门（如单位 `$_NOT_`），产出的门级网络随后通常交给 `abc` 做工艺映射；并强调**本库不映射 `$mem`**——`$mem` 必须先用 `memory_map` 拆成逻辑和 `$dff`。这条边界承接 u6-l4。

**模式 1：委托 simplemap 的空模块**：

[techmap.v:41-69](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/techmap.v#L41-L69) —— 这几个模块体是空的（`module ...; endmodule`），仅靠 `(* techmap_simplemap *)` 和 `(* techmap_celltype = "..." *)` 两个属性告诉 `techmap`：凡是列出的类型，都转给 `simplemap`。比如 `$not $and $or $xor $xnor`、比较 `$eq $ne ...`、多路 `$mux $pmux ...`、寄存器 `$dff $adff ...`。

**模式 2：行为级模板 + `_TECHMAP_DO_`**：

[techmap.v:98-99](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/techmap.v#L98-L99) —— 移位单元 `$shr/$shl/...` 用 `always @*` + `for` 循环写出桶形移位的行为级实现，然后 `_TECHMAP_DO_00_ = "proc;;"` 让 `techmap` 自动跑 `proc`（u6-l2）把它翻成 `$mux`，`_TECHMAP_DO_01_` 再跑 `opt_muxtree; opt_expr -fine` 做优化。这正是「模板里写行为级，靠 `_TECHMAP_DO_` 自动门级化」的范本。

**全加器 `$fa` 的实现**：

[techmap.v:193-207](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/techmap.v#L193-L207) —— 用纯组合逻辑 `assign t1 = A ^ B; ... assign Y = t1 ^ C; assign X = t2 | t3;` 实现一位全加器。注意它产出的是更小的 `$xor`/`$and`/`$or`，这些随后又被模式 1 的 simplemap 拆成 `$_XOR_`/`$_AND_`/`$_OR_`。这展示了「一个模板可以产出另一种待映射单元，由不动点循环继续往下拆」。

**模式 3：算术单元委托 `alumacc`**：

[techmap.v:299-307](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/techmap.v#L299-L307) —— `$macc/$macc_v2` 挂 `techmap_maccmap`（交给 `maccmap`）；`$lt $le $ge $gt $add $sub $neg $mul` 挂 `techmap_wrap = "alumacc"`。这两条规则合起来，是算术单元能被 `techmap` 处理的全部秘密。

**`simlib.v` 头部自述**：

[simlib.v:18-31](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/simlib.v#L18-L31) —— 自述为「仿真库」，提供 `$not` 等内部 cell 的**简单仿真模型**，用来验证前端/pass 产出的网表功能是否正确。注意它**不是映射库**。`$not` 的行为级模型见 [simlib.v:48-65](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/simlib.v#L48-L65)，用 `assign Y = ~A;` 表达。

**`simcells.v` 头部自述**：

[simcells.v:20-26](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/simcells.v#L20-L26) —— 自述为「门级 cell 仿真库」，提供 `$_NOT_`/`$_AND_` 等**默认工艺映射器（techmap.v）产出、且 `abc` 所期望**的门级原语模型。`$_NOT_` 的模型见 [simcells.v:58-62](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/simcells.v#L58-L62)。这把 techmap 的「产出目标」和 abc 的「输入约定」对上了。

#### 4.2.4 代码实践

**实践目标**：对比「实现库」与「仿真库」对同一个 `$not` 的写法差异，建立直觉。

**操作步骤**：

1. 打开 [techlibs/common/techmap.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/techmap.v)，找到 `$not` 的映射规则（它在 `_90_simplemap_bool_ops` 这个空模块的 `techmap_celltype` 列表里，第 [42](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/techmap.v#L42) 行）——它**没有给出实现体**，而是委托给 simplemap。
2. 打开 [techlibs/common/simlib.v](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/simlib.v) 第 [48-65](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/simlib.v#L48-L65) 行，看 `$not` 的**仿真模型** `assign Y = ~A;`。
3. 思考：为什么 `$not` 的映射规则体是空的？

**需要观察的现象**：techmap.v 里 `$not` 没有写任何逻辑，只挂了属性；simlib.v 里 `$not` 写了完整的 `~A` 行为。

**预期结果**：因为「逐位取反」是完全机械的操作，交给 C++ 的 `simplemap`（4.3 节）一次性拆成 `$_NOT_` 门，比用 Verilog 写模板再跑 `proc` 更快更省事——所以 techmap.v 选择「委托」。而 simlib.v 不参与映射，必须自己写出 `~A` 才能仿真。

#### 4.2.5 小练习与答案

**练习 1**：如果我想让 yosys 支持一种新的自定义高层单元 `$mygate`，并希望 `techmap` 能把它映射到 `$_AND_`，我需要改 C++ 代码吗？

> **答案**：不需要。只要写一个 `.v` 文件，里面定义一个带 `(* techmap_celltype = "$mygate" *)` 的模块，模块体内用 `$_AND_` 实现 `$mygate` 的功能，然后用 `techmap -map mymap.v` 加载即可。这正是「数据驱动映射」的好处。

**练习 2**：`_TECHMAP_FAIL_` 机制有什么用？给一个使用场景。

> **答案**：当一个 cell 类型有多个候选模板时，`_TECHMAP_FAIL_` 让某个模板「在特定条件下主动退出」。典型场景：某乘法器模板只在操作数位宽 ≤ 16 时高效，于是模板里写 `_TECHMAP_FAIL_ = (WIDTH > 16);`，位宽过大时该模板失败，`techmap` 自动尝试下一个更通用的候选模板。

---

### 4.3 simplemap：最简单的门级展开

#### 4.3.1 概念说明

`simplemap` 是与 `techmap` 完全不同的另一条映射路径：它是**写死在 C++ 里**的，针对一批「机械」单元，直接在代码里造出对应的 `$_` 门。

为什么需要它？因为像 `$and`、`$or`、`$mux`、`$dff` 这类单元，映射规则极其简单且固定——「N 位的 `$and` 就是 N 个 1 位的 `$_AND_`」。用 Verilog 模板 + 跑 `proc` 来做这件事，开销大、没必要；直接用 C++ 一个 `for` 循环逐位造门，又快又省。所以 `simplemap` 本质上是「为最常见的一批单元做的硬编码快速通道」。

**`simplemap` 与 `techmap` 的关系**（重要）：

- `simplemap` 既是**一条独立命令**（`SimplemapPass`），又是一组**可被 `techmap` 复用的函数**（通过 `simplemap_get_mappers` 暴露）。
- 当 `techmap` 遇到带 `techmap_simplemap` 属性的模板时，它**不内联模板内容**，而是直接调用对应的 simplemap 函数来完成映射（见 [techmap.cc:582-596](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/techmap.cc#L582-L596)）。也就是说，4.2 节里那些「委托 simplemap 的空模块」其实是 `techmap` 在「点名」让 simplemap 函数上场。

**`simplemap` 的能力边界**（更重要）：

`simplemap` 只认一批**位运算、归约、逻辑、比较、多路、寄存器**单元。它**完全不处理算术单元**——`$mul`、`$add`、`$sub`、`$div` 都不在它的表里。这是它与 `techmap` 最实用的区别：对含算术的设计，`techmap` 能（经 `alumacc`+`maccmap`）映射，`simplemap` 会**原样留下**它们不动。

#### 4.3.2 核心流程

`simplemap` 的核心算法是**逐位分解（bit-slicing）**。其数学原理很朴素：对于一个逐位运算的 N 位单元，输出 Y 的第 i 位只依赖于输入的第 i 位。以 `$and` 为例：

\[ Y_i = A_i \wedge B_i, \quad i = 0, 1, \dots, N-1 \]

因此一个 N 位 `$and` 可以等价展开为 N 个独立的 1 位 `$_AND_` 门，彼此毫无数据依赖。`simplemap` 的每个 mapper 函数就是把这个分解直接翻译成 `module->addCell(...)` 调用。对于归约运算（`$reduce_or` 等），则用一棵二叉门树把 N 个输入归约成 1 位输出。

`simplemap` 命令的执行流程：

1. `simplemap_get_mappers` 建一张「cell 类型 → mapper 函数指针」的表（命令启动时建一次）。
2. 遍历设计每个非 blackbox 模块的每个 cell。
3. 若 cell 类型在表里，调用对应 mapper（在当前模块里造出一批 `$_` 门），然后删除原 cell。

#### 4.3.3 源码精读

**位运算映射 `simplemap_bitop`**：

[simplemap.cc:86-112](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/simplemap.cc#L86-L112) —— 这是逐位分解的范本。先按符号位把 `A`/`B` 宽度对齐到 `Y`，再按 cell 类型选出门类型（`$and→$_AND_`、`$or→$_OR_`、`$xor→$_XOR_`、`$xnor→$_XNOR_`），最后 `for` 循环每一位 `module->addCell(NEW_ID, gate_type)` 造一个门并接好 `A[i]/B[i]/Y[i]`。`transfer_src` 把原 cell 的 `src` 源码位置属性透传给新门，方便调试定位（见 [simplemap.cc:34-36](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/simplemap.cc#L34-L36)）。

**多路器映射 `simplemap_mux`**：

[simplemap.cc:291-305](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/simplemap.cc#L291-L305) —— N 位 `$mux` 拆成 N 个 `$_MUX_`，共享同一个选择信号 `S`，每位独立选 `A[i]` 或 `B[i]`。

**寄存器映射 `simplemap_ff`**：

[simplemap.cc:431-439](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/simplemap.cc#L431-L439) —— 触发器/锁存器家族（`$dff`/`$adff`/`$dlatch`/...）统一走 `FfData` 辅助类（`kernel/ff.h`），逐位 slice 出单位 `$_DFF_*_` 等门。这比手写每一种 FF 变体简洁得多。

**映射表 `simplemap_get_mappers`**：

[simplemap.cc:483-530](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/simplemap.cc#L483-L530) —— 这张表是 `simplemap` 能力的**权威清单**。仔细看：里面有 `$not/$and/$or/$xor/$xnor`、各种 `$reduce_*`、`$logic_*`、`$eq/$ne`、`$mux/$pmux/$bwmux`、`$lut/$sop`、`$slice/$concat`，以及一大串 `$sr/$ff/$dff/$dffe/$adff/.../$dlatch`。**但绝没有 `$mul`、`$add`、`$sub`、`$div`、`$mod`**——这就是 `simplemap` 不碰算术的铁证。

**`SimplemapPass::execute`**：

[simplemap.cc:566-588](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/simplemap.cc#L566-L588) —— 遍历模块和 cell，命中表就调 mapper 并 `mod->remove(cell)`。help 文本（[simplemap.cc:559-563](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/simplemap.cc#L559-L563)）列出的能映射类型，与上表完全一致。

**`techmap` 内部对 simplemap 的复用**：

[techmap.cc:582-596](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/techmap.cc#L582-L596) —— 当 `extmapper_name == "simplemap"` 时，`techmap` 直接调用 `simplemap_mappers.at(cell->type)(module, cell)` 在当前模块就地映射，再 `module->remove(cell)`。这正是 4.2 节那些「空模板」被点名的实际效果。注意 `techmap` 在 [techmap.cc:1148](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/techmap.cc#L1148) 通过 `simplemap_get_mappers(worker.simplemap_mappers)` 把这张表搬进自己体内。

#### 4.3.4 代码实践

**实践目标**：对比 `techmap` 与 `simplemap` 在**算术单元**上的行为差异，验证「simplemap 不处理算术」。

**操作步骤**：

1. 准备 `arith.v`：

```verilog
// 示例代码
module arith(input [3:0] a, b, output [7:0] y);
    assign y = a * b;     // 产生 $mul
endmodule
```

2. 在两个**独立的** yosys 会话里分别跑（避免互相污染设计）：

```
# 会话 A：用 techmap
yosys> read_verilog arith.v
yosys> proc; opt_clean
yosys> techmap
yosys> stat

# 会话 B：用 simplemap
yosys> read_verilog arith.v
yosys> proc; opt_clean
yosys> simplemap
yosys> stat
```

**需要观察的现象**：
- 会话 A（`techmap`）：`$mul` 消失，被替换为一堆 `$and`（乘法的部分积）和 `$fa`/`$xor`/`$or`（加法树），最终落成 `$_AND_`/`$_XOR_`/`$_OR_` 等门（具体组合与位宽有关）。
- 会话 B（`simplemap`）：`$mul` **原封不动还在**，因为 `simplemap` 表里没有 `$mul`。

**预期结果**：会话 A 的 `stat` 里不再有 `$mul`，出现大量 `$_` 门；会话 B 的 `stat` 里仍能看到 `$mul` cells: 1，且几乎没有新增 `$_` 门。这一对比直观说明：**算术映射必须走 techmap（及其 alumacc/maccmap 链），simplemap 无能为力**。

> 具体生成的门种类与数量为「待本地验证」，但「techmap 能消掉 `$mul`、simplemap 不能」这一结论是确定的。

#### 4.3.5 小练习与答案

**练习 1**：既然 `simplemap` 能做的事 `techmap` 也能做（通过 `techmap_simplemap` 委托），为什么 yosys 还要保留 `simplemap` 这条独立命令？

> **答案**：①性能：`simplemap` 直接调 C++ 函数造门，省去了 `techmap` 加载库、建索引、派生模板、内联等一系列开销，对这批「机械」单元更快；②复用：`simplemap` 的 mapper 函数被 `techmap`、`maccmap` 等多处复用，是共享的基础设施；③教学/调试：作为独立命令，方便单独观察「逐位拆分」的效果，也便于在自定义脚本里对选中单元快速降级到门级。

**练习 2**：`simplemap` 把 `$and` 拆成 `$_AND_` 时，为什么要 `transfer_src` 透传 `src` 属性？

> **答案**：`src` 属性记录了 cell 对应的源码文件和行号（u2/u3 讲过属性系统）。拆分后原 `$and` 没了，若不把 `src` 传给新生成的 `$_AND_` 门，调试和报错时就丢失了「这门来自哪行 Verilog」的信息，不利于定位问题。

## 5. 综合实践：跟踪 `$mul` 从高层到门级的完整下沉链

把本讲三个模块串起来，做一个贯穿性任务：观察一个 `$mul` 单元是如何被 `techmap` 一层层替换到门级原语的。

**设计文件 `muladd.v`**：

```verilog
// 示例代码
module muladd(input [3:0] a, b, input [7:0] c, output [7:0] y);
    assign y = a * b + c;   // 同时含 $mul 与 $add
endmodule
```

**操作步骤**：

1. 读入并先做行为级清理，确认起点：

```
yosys> read_verilog muladd.v
yosys> proc; opt_clean
yosys> stat                 # 应能看到 $mul 和 $add 各 1 个
yosys> write_rtlil step0.il
```

2. 只跑一步算术归并，观察中间形态（**不**直接 techmap，先看 alumacc 的作用）：

```
yosys> alumacc
yosys> stat                 # $mul/$add 消失，出现 $macc（乘加合一）
yosys> write_rtlil step1.il
```

3. 继续 `techmap`，让 `$macc` 经 maccmap 落地、`$fa` 等再被拆开：

```
yosys> techmap
yosys> stat                 # 出现 $and（部分积）/$fa/$xor 等，最终落到 $_AND_/$_XOR_/$_OR_
yosys> write_rtlil step2.il
```

4. 对照源码理解每一步的「为什么」：
   - step0→step1：`alumacc` 把 `$mul+$add` 归并成 `$macc`。这正对应 `techmap.v` 里 [techmap.v:304-307](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/techmap.v#L304-L307) 的 `techmap_wrap = "alumacc"` 规则——当你直接跑 `techmap`（不手动 alumacc）时，techmap 会用 wrap 机制**自动**替你做这一步。
   - step1→step2 中的 `$macc → $and/$fa`：对应 [techmap.v:299-302](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/techmap.v#L299-L302) 的 `techmap_maccmap`，由 `maccmap` 用 `module->And(...)` 造部分积、用 `$fa` 造加法树（见 [maccmap.cc:82](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/maccmap.cc#L82) 与 [maccmap.cc:114](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/maccmap.cc#L114)）。
   - step2 中的 `$fa → $xor/$and/$or`：对应 [techmap.v:193-207](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/techmap.v#L193-L207)。
   - 最后 `$xor/$and/$or → $_XOR_/$_AND_/$_OR_`：对应 `simplemap` 的逐位分解（[simplemap.cc:86-112](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/techmap/simplemap.cc#L86-L112)），由 techmap 经 `techmap_simplemap` 委托触发。

5. **对照 synth 流程定位**：在标准 `synth` 里，`alumacc` 在 coarse 阶段先跑（[synth.cc:308-309](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc#L308-L309)），`techmap` 在 fine 阶段跑（[synth.cc:321-332](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc#L321-L332)，紧跟 `memory_map` 之后）。所以你手动跑的 step1（alumacc）+ step2（techmap）正好复现了 synth 的内部顺序。

**需要观察的现象**：每一步 `stat` 输出的单元类型与数量都在变化，体现「不动点循环」——techmap 每替换出新的可映射单元（`$macc`→`$fa`→`$xor`），就再迭代一轮，直到全部是 simplemap 产出的 `$_` 门为止。

**预期结果**：step2 的 `stat` 里 `$mul`/`$add`/`$macc`/`$fa` 全部消失，只剩 `$_AND_`/`$_XOR_`/`$_OR_` 等门级原语（以及可能的 `$_DFF_`，本例无时序故无）。

> 各步具体单元数量为「待本地验证」，但「`$mul → $macc → $and/$fa → $_ 门`」这条下沉链是确定的。

## 6. 本讲小结

- **`techmap` 是一台不懂 RTLIL 语义的「模板替换机器」**：它加载映射库（默认 `+/techmap.v`），按 cell 类型匹配模板模块，把模板内容内联进当前模块并删除原 cell，循环到不动点。映射规则全是普通 `.v` 模块，新增映射无需改 C++。
- **`techmap.v` / `simlib.v` / `simcells.v` 三者职责不同**：techmap.v 是「实现库」（怎么造门），simlib.v 是高层 `$` 单元的「仿真模型」，simcells.v 是门级 `$_` 单元的「仿真模型」且是 `abc` 的输入约定。映射只读 techmap.v。
- **模板靠属性与特殊线网驱动 techmap**：`techmap_celltype` 决定匹配范围；`techmap_simplemap`/`techmap_maccmap`/`techmap_wrap` 决定委托给谁；`_TECHMAP_DO_*` 让模板里的行为级代码自动被 `proc`/`opt` 门级化；`_TECHMAP_FAIL_` 让模板有条件退出。
- **`simplemap` 是写死在 C++ 里的快速通道**：用「逐位分解」把 `$and`/`$or`/`$mux`/`$dff` 等机械单元直接拆成 `$_` 门，又快又省；它既是独立命令，也被 `techmap` 通过 `techmap_simplemap` 复用。
- **算术单元只能走 techmap**：`$mul`/`$add` 经 `techmap_wrap "alumacc"` → `$macc` → `techmap_maccmap`(maccmap) → `$and`/`$fa` → `$_` 门；`simplemap` 表里没有算术单元，对它们原样保留不动。
- **在 `synth` 里的位置**：`alumacc` 在 coarse 阶段，`techmap` 在 fine 阶段（紧跟 `memory_map`），二者把 u6-l2~u6-l4 产出的高层 `$` 网表最终下沉到门级。

## 7. 下一步学习建议

- **下一讲 u6-l6（abc9 与 liberty）**：本讲把设计降到了 `$_AND_`/`$_OR_`/`$_NOT_` 这类「与工艺无关」的门级原语。下一讲将介绍 `abc9` 如何在此基础上做**布尔网络优化与标准单元/LUT 映射**，把通用门映射到具体工艺库（liberty），以及 `dfflibmap` 如何把 `$dff`/`$_DFF_` 映射到库里的具体触发器。这是「门级 → 工艺单元」的最后一公里。
- **延伸阅读**：
  - `passes/techmap/maccmap.cc`：理解 `$macc` 是如何被分解为 `$and` 部分积与 `$fa` 加法树的，补全本讲对乘法下沉链的细节。
  - `passes/techmap/abc9.cc`：预告下一讲，看 techmap 产出的 AIG 如何交给 ABC。
  - `techlibs/common/techmap.v` 的 `$alu`/`$lcu` 模板：进阶理解算术与超前进位链的模板写法。
  - 动手实验：写一个带 `techmap_celltype` 的自定义映射模块，用 `techmap -map` 加载，验证「数据驱动映射」的可扩展性（对应 u9-l1 自定义 pass 的前置练习）。
