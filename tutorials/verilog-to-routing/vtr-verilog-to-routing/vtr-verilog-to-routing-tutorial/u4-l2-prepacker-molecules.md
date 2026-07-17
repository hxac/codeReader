# Prepacker 与打包分子

## 1. 本讲目标

上一讲（u4-l1）我们看清了打包的骨架：`try_pack` 用「先紧后松」的状态机反复调用 `GreedyClusterer`，把原子网表 `AtomNetlist` 装箱成聚簇网表 `ClusteredNetlist`。但聚簇器并不是直接面对一个个孤立的 LUT、FF、加法器，它面对的是 **打包分子（molecule）**。

本讲就回答一个核心问题：**分子从哪里来？谁把原子组合成了分子？**

读完本讲，你应该能够：

1. 说清 **pack pattern（打包模式）** 这个架构概念：它在 XML 里长什么样、解决什么问题。
2. 跟踪 `Prepacker` 如何把架构里的 pack pattern 识别成 `t_pack_patterns`，再如何在网表里「套用」这些模式生成 `t_pack_molecule`，尤其理解 **进位链（carry chain）** 如何把一串加法器原子绑成一个分子。
3. 区分两类分子：单原子分子（`MOLECULE_SINGLE_ATOM`）与强制打包分子（`MOLECULE_FORCED_PACK`）。
4. 理解 `Prepacker` 给每个原子预算的「期望最低代价 PB 节点」，以及聚簇阶段落地后由 `AtomPBBimap` 维护的「原子 ↔ 物理块」双向绑定。

> 承接 u4-l1：`vpr_pack` 在调用 `try_pack` 之前，会先构造一个 `Prepacker` 对象。本讲就把这个对象彻底拆开。

## 2. 前置知识

- **AtomNetlist（u3-l2）**：技术映射后的原子级网表，块是 LUT/FF/加法器等原语，块之间用线网连接。本讲的所有「原子」都指 `AtomBlockId`。
- **t_pb_type 与 PB 图（u2-l2、u4-l1）**：架构里逻辑块内部的层次化原语树；`t_pb_graph_node` 是把这棵树按 `num_pb` 展开后的可布线实例模板。pack pattern 标注在这张图的边上。
- **StrongId（u3-l1）**：编译期区分类型的轻量 ID。本讲会出现 `PackMoleculeId`、`MoleculeChainId`、`AtomBlockId`，它们互不相容，不会误用。
- 直觉概念：**分子（molecule）** 是「必须一起放进同一个逻辑块的一组原子」。它是聚簇的基本单元——聚簇器一次搬动一个分子，而不是一个原子。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [vpr/src/pack/prepack.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.h) | `Prepacker` 类、`t_pack_molecule`、`PackMoleculeId`/`MoleculeChainId` 的声明，是本讲的核心头文件。 |
| [vpr/src/pack/prepack.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp) | 分子识别与构建的全部实现：`alloc_and_load_pack_patterns`、`alloc_and_load_pack_molecules`、`try_create_molecule`、`try_expand_molecule`、进位链相关函数。 |
| [vpr/src/pack/pack_patterns.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack_patterns.h) | `t_pack_patterns`、`t_pack_pattern_block`、`t_pack_pattern_connections` 的数据结构定义。 |
| [vpr/src/pack/pb_type_graph.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pb_type_graph.h) | PB 图构建入口 `alloc_and_load_all_pb_graphs`，pack pattern 就标注在这张图的边上。 |
| [vpr/src/pack/atom_pb_bimap.h](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/atom_pb_bimap.h) | `AtomPBBimap`：聚簇落地后维护的「原子 ↔ 物理块」双向映射。 |
| [vpr/src/base/vpr_api.cpp](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp) | `vpr_pack` 在此处构造 `Prepacker`，把本讲与上一讲的主流程接起来。 |

---

## 4. 核心概念与源码讲解

### 4.1 pack pattern 识别：从架构 XML 到 t_pack_patterns

#### 4.1.1 概念说明

**pack pattern（打包模式）** 是架构文件作者给打包器的一个「强烈建议」：在逻辑块内部，某些原语之间存在一条特别值得保持相邻的连线。两个经典例子（见 [prepack.h:1-9](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.h#L1-L9) 的注释）：

1. **强制对（forced pack）**：例如 6-LUT 紧跟一个 FF，二者在网表里顺序相连，就应该塞进同一个逻辑单元，提升打包密度。
2. **进位链（carry chain）**：一串加法器之间用专用走线（`Cout → Cin`）串起来。只有把它们放进按链序排列的逻辑块，才能用上这条专用布线。

注意它是 **架构概念**，不是网表概念——模式定义在架构 XML 的 `<interconnect>` 里，标注在某条 `<direct>` 连线边上：

```xml
<!-- 进位链示例：摘自 7series_BRAM_DSP_carry.xml -->
<direct name="cin" input="SLICE_L.cin" output="fle[0].cin">
  <pack_pattern name="chain" in_port="SLICE_L.cin" out_port="fle[0].cin" />
</direct>
<direct name="Acarry" input="fle[0].cout" output="fle[1].cin">
  <pack_pattern name="chain" in_port="fle[0].cout" out_port="fle[1].cin" />
</direct>
<!-- ... fle[1].cout -> fle[2].cin -> fle[3].cin -> SLICE_L.cout ... -->
```

> 参见 [vtr_flow/arch/xilinx/7series_BRAM_DSP_carry.xml:1418-1432](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/arch/xilinx/7series_BRAM_DSP_carry.xml#L1418-L1432)。

同一个 `name="chain"` 的几条 `<pack_pattern>` 标注，共同勾勒出「从簇输入 `cin` 一路串到簇输出 `cout`」的一条模式。`SLICE_L.cin`/`SLICE_L.cout` 是 **根块（root block，即逻辑块顶层）的引脚**——这一点决定了它是一条 **跨块链**（chain），下文会反复用到。

#### 4.1.2 核心流程：识别模式的三步

架构 XML 在 u2 阶段解析为 `t_pb_type` 树；u4-l1 讲过 PB 图由 `alloc_and_load_all_pb_graphs`（[pb_type_graph.h:20](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pb_type_graph.h#L20)）展开成 `t_pb_graph_node`。每条 PB 图边 `t_pb_graph_edge` 上会带上架构里标注的 `pack_pattern_names`（模式名数组）。`Prepacker` 构造时，`alloc_and_load_pack_patterns` 就在这张现成的 PB 图上做三件事：

```text
Step 1  发现：遍历所有 PB 图边，把标注过的模式名收集进哈希表 pattern_names
        （discover_pattern_names_in_pb_graph_node）
Step 2  初始化：为每个模式名建一个空的 t_pack_patterns（root_block = nullptr）
Step 3  扩展：对每个模式，找到一条属于它的「扩展边」，从这条边向前/向后
        把整条模式上的原语块与连线建出来（forward/backward_expand_pack_pattern_from_edge）
        —— 同时判定它是不是跨块链 is_chain
```

#### 4.1.3 源码精读

**Step 1+2：发现模式名并初始化模式列表。** `discover_pattern_names_in_pb_graph_node` 递归遍历 PB 图的输入/输出/时钟引脚的出边，把边的 `pack_pattern_names` 插入哈希表，新名字自动获得递增下标（[prepack.cpp:225-343](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L225-L343)）。这里还有一个 **模式推断（infer）** 的小优化：当一条边的输出只有唯一去向，或输入只有唯一来源时，即使架构作者没显式标注，也自动把这条「显而易见」的连线归入模式（`forward_infer_pattern` / `backward_infer_pattern`，[prepack.cpp:348-363](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L348-L363)）——这样作者写模式时可以省略中间冗余的直连。

**Step 3：扩展边并构建 `t_pack_patterns`。** 顶层循环对每个模式索引找一条扩展边，然后调用 `backward_expand_pack_pattern_from_edge`（[prepack.cpp:181-184](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L181-L184)）：

> [prepack.cpp:153-219](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L153-L219) —— `alloc_and_load_pack_patterns`：核心是「按模式索引找扩展边 → 前后扩展建出模式的块与连线 → 设置每个块是否可选」。

`backward_expand_pack_pattern_from_edge` 沿着边的驱动方向回溯，每遇到一个原语（`is_primitive_pin()` 为真的引脚所属节点）就新建一个 `t_pack_pattern_block`，并递归探索它的输入/输出/时钟边。**链判定** 就发生在这里——当回溯到一条「没有驱动边、且其父节点是根块（`is_root()`）的输入引脚」时，说明这条模式一直延伸到了逻辑块的顶层输入，它会跨越多个逻辑块，于是置 `is_chain = true`（[prepack.cpp:758-769](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L758-L769)）：

```cpp
// 摘自 prepack.cpp:758-769 —— 判定链：模式的输入引脚没有驱动且来自根块
if (expansion_edge->input_pins[i]->num_input_edges == 0) {
    if (expansion_edge->input_pins[i]->parent_node->pb_type->is_root()) {
        // 该 pack pattern 延伸到了 CLB（根块）输入引脚，
        // 因此它会跨越多个逻辑块，按链处理
        packing_pattern.is_chain = true;
        forward_expand_pack_pattern_from_edge(..., true /*make_root_of_chain*/, ...);
    }
}
```

**链的可选性。** 链（如进位链）长度可变——网表里可能只有 1 个加法器，也可能有 10 个。所以链模式中除根块外的每个块都被标记为 **可选**（`is_block_optional[k] = true`），而非链模式则要求网表必须匹配上全部块才能成分子（[prepack.cpp:191-197](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L191-L197)）。这个 `is_block_optional` 数组在 4.2 节匹配时是「合法性」的关键。

最终的 `t_pack_patterns` 结构（[pack_patterns.h:94-117](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack_patterns.h#L94-L117)）记住几个字段：

| 字段 | 含义 |
|------|------|
| `root_block` | 模式的起点块（链里是被簇输入驱动的那个原语） |
| `num_blocks` | 模式包含的原语个数 |
| `is_block_optional[]` | 该位置是否允许空缺（链中段为 true） |
| `is_chain` | 是否跨逻辑块的链 |
| `chain_root_pins` | 仅链非空：簇输入连到链首原语的引脚，决定链从哪起 |
| `base_cost` | 模式内所有原语 `compute_primitive_base_cost` 之和 |

#### 4.1.4 代码实践：数一数架构里有几个模式

1. **实践目标**：亲手从架构 XML 追到 `t_pack_patterns`，建立「XML 标注 ↔ 内存结构」的对应。
2. **操作步骤**：
   - 打开 [vtr_flow/arch/xilinx/7series_BRAM_DSP_carry.xml](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/arch/xilinx/7series_BRAM_DSP_carry.xml)，搜索 `pack_pattern`，统计出现的不重复 `name`（应有 `chain`、`ff_in` 等）。
   - 打开 [prepack.cpp:153-219](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L153-L219)，确认 `pattern_names` 哈希表的大小就等于你数出的模式个数。
3. **需要观察的现象**：同名的多条 `<pack_pattern>`（如多条 `name="chain"`）会被合并成 **一个** `t_pack_patterns`（同一个 `index`），而不是每条 XML 标注一个模式。
4. **预期结果**：你数出的不重复模式名个数 == `list_of_pack_patterns.size()`。其中 `chain` 的 `is_chain == true`、中段块 `is_block_optional` 为真；`ff_in`（LUT→FF）的 `is_chain == false`、所有块都必填。
5. 若本地已构建，可开 echo 验证：见 4.2.4。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `discover_pattern_names_in_pb_graph_node` 要对输入、输出、时钟三种引脚分别循环？能否只看输入引脚？
  - **答案**：模式可以挂在任意方向的边上。LUT→FF 的输出边、加法器链的输出边、时钟到 FF 的边都可能标注模式。只看输入引脚会漏掉那些从输出/时钟侧才能发现的标注。
- **练习 2**：`infer_pattern`（模式推断）如果不做，架构作者要多写什么？
  - **答案**：要把链上每一段 `cout→cin` 直连都显式标 `<pack_pattern>`。有了推断，作者只需标关键边，中间的唯一直连会被自动并入同模式。

---

### 4.2 分子构建：把原子组合成打包分子

#### 4.2.1 概念说明

有了 `t_pack_patterns`（架构里有哪些「好搭档」组合），下一步是 **在网表里套用这些模式**，把命中的原子粘成 **分子**。分子是聚簇的原子单元——聚簇器以分子为单位搬进逻辑块，保证模式里的原子不会被打散。

分子的完整定义见 [prepack.h:69-114](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.h#L69-L114) 的 `t_pack_molecule`，关键成员：

| 成员 | 含义 |
|------|------|
| `type` | `MOLECULE_SINGLE_ATOM`（单原子）或 `MOLECULE_FORCED_PACK`（多原子强制打包） |
| `pack_pattern` | 若是强制打包分子，指向它匹配的 `t_pack_patterns`；单原子分子为 `nullptr` |
| `atom_block_ids` | 按 `block_id` 索引的原子数组，未填的位置是 `INVALID`（链可不满） |
| `root` | 模式根块在 `atom_block_ids` 中的下标 |
| `base_gain` | 分子的「内在好感度」，用于聚簇种子排序 |
| `chain_id` | 若属于链，指向共享的 `t_chain_info`；否则 `INVALID` |
| `is_chain()` | 是强制打包 **且** 其模式 `is_chain==true` |

两类分子的来源见 [prepack.h:332-342](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.h#L332-L342) 的注释：①单原子天然是分子；②匹配上 `t_pack_patterns` 的一组原子是强制打包分子；③链是一种特殊的强制打包分子，可被切分到多个逻辑块。

#### 4.2.2 核心流程：从模式到分子

`Prepacker` 构造函数（[prepack.cpp:1772-1798](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L1772-L1798)）依次做两件大事：先 `alloc_and_load_pack_patterns`（4.1 节）拿到所有模式，再 `alloc_and_load_pack_molecules` 用这些模式在网表里生成分子。后者的主循环逻辑：

```text
对每个模式（按"大模式优先、低代价优先"排序）：
    若该模式根块所在 mode 被禁用打包(disable_packing)，跳过
    对网表中每个原子 blk_id：
        用 blk_id 当根，尝试建分子 try_create_molecule
        若成功：
            记 base_gain；把分子里每个原子登记进 atom_molecules_multimap
            若该原子未被本分子覆盖（链分子可能只覆盖中段）→ 回退迭代器再试
# 收尾：遍历所有原子，凡还不在任何分子里的，各建一个 MOLECULE_SINGLE_ATOM 分子
```

模式之间有 **优先级**：`compare_pack_pattern`（[prepack.cpp:1368-1390](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L1368-L1390)）规定 **块数多的模式优先**（更难塞、先占位），块数相同则 **代价低的优先**。每个原子采用 **first-fit（首次匹配）**：一旦被某个分子占用，就不再参与更靠后的模式——所以模式处理顺序很关键。

> 注意一个不变式（[prepack.cpp:1787-1797](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L1787-L1797)）：构造结束后，**每个原子恰好属于一个分子**。多对多的中间态 `atom_molecules_multimap` 会被收敛成一一对应的 `atom_molecule_`。

#### 4.2.3 源码精读

**（a）建一个分子：`try_create_molecule`。** 给定模式索引与一个候选根原子，先做链的特殊处理，再调 `try_expand_molecule` 把模式的每个槽位填上原子（[prepack.cpp:980-1033](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L980-L1033)）：

```cpp
// 摘自 prepack.cpp:993-1005 —— 链要先找到链头，再开始填
if (pack_pattern->is_chain) {
    blk_id = find_new_root_atom_for_chain(blk_id, pack_pattern, ...);
    if (!blk_id) return PackMoleculeId::INVALID();
}
t_pack_molecule molecule;
molecule.type = e_pack_pattern_molecule_type::MOLECULE_FORCED_PACK;
molecule.pack_pattern = pack_pattern;
molecule.atom_block_ids = std::vector<AtomBlockId>(pack_pattern->num_blocks); // 全 INVALID
molecule.root = pack_pattern->root_block->block_id;
```

**（b）进位链如何绑定多个原子：链头回溯 + BFS 填槽。** 这是本讲的核心。两步：

1. **`find_new_root_atom_for_chain`**（[prepack.cpp:1400-1445](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L1400-L1445)）：给定链中任意一个加法器原子，沿 `Cin` 的驱动方向 **一路向上回溯**，找到链中最上游、且尚未归属任何分子的那个原子，作为这个分子的真正根。这样链被切成「每段从一个真正的链头开始」的分子，避免从链中间截断。

   ```cpp
   // 摘自 prepack.cpp:1422-1435 —— 找链最上游未占用的原子当根
   AtomBlockId driver_blk_id = atom_nlist.find_atom_pin_driver(blk_id, model_port, root_ipin->pin_number);
   if (!driver_blk_id)              return blk_id;   // 没有驱动 → 自己就是链头
   if (atom_molecules 含 driver_blk_id) return blk_id; // 驱动已被占 → 自己是新段链头
   return find_new_root_atom_for_chain(driver_blk_id, ...); // 否则继续上溯
   ```

2. **`try_expand_molecule`**（[prepack.cpp:1048-1128](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L1048-L1128)）：以根原子为起点，用 **广度优先** 同时遍历「模式的块连接图」和「原子网表」，把相邻原子逐个填进对应槽位。队列里放 `(模式块, 候选原子)` 对，每次出队检查：原子能否放进该原语类型（`primitive_type_feasible`）、该槽是否已被占、该原子是否已属别的分子。若某必填槽填不上 → 整个分子失败返回 `false`；若是可选槽（链中段）→ 跳过继续。

   邻接关系靠 `get_sink_block`/`get_driving_block`（[prepack.cpp:1138-1226](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L1138-L1226)）在网表里查「这条模式连线对应的线网，连到了哪个对端原子」。代码里有一条重要简化假设：**强制打包网默认单扇出**（注释见 [prepack.h:349-354](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.h#L349-L354)），即一个原语的输出在模式里只驱动一个对端。

**（c）链的归属与长链标记：`init_molecule_chain_info`。** 分子建好后，若它是链分子，还要决定它属于哪条链（[prepack.cpp:1705-1748](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L1705-L1748)）：若当前分子的链头原子 **没有** 上游驱动原子、或驱动原子尚未归任何分子 → 这是 **新链**，分配新 `MoleculeChainId`；否则它接在驱动原子所属分子的同一条链上，并标记该链 `is_long_chain = true`（跨多个逻辑块）。这样一条跨越多块的长进位链，会被切成多个分子，但共享同一个 `chain_id`，布局时就能把这条链摆在相邻逻辑块上。

**（d）base_gain 打分。** 强制打包分子的好感度（[prepack.cpp:884](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L884)）：

\[ \text{base\_gain} = N_{\text{atoms}} - \frac{\text{base\_cost}}{100} \]

原子越多（分子越大）越值得先打包；模式的物理代价越高则略减好感度。单原子分子 `base_gain = 1`（[prepack.cpp:932](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L932)）。这个分数在 u4-l3 聚簇种子选择时派上用场。

**（e）单原子兜底。** 主循环只生成强制打包分子。收尾循环（[prepack.cpp:910-939](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L910-L939)）给每个尚未归属的原子建一个 `MOLECULE_SINGLE_ATOM` 分子，`atom_block_ids = {blk_id}`、`chain_id = INVALID`。这一步保证不变式「每个原子恰在一个分子里」。同一收尾循环还顺手为每个原子预算 `expected_lowest_cost_pb_gnode`（见 4.3）。

#### 4.2.4 代码实践：跟踪进位链如何绑定多个原子（本讲指定实践）

1. **实践目标**：在 `prepack.cpp` 中找到构建分子的核心函数，说清进位链如何把多个加法器原子绑成一个分子。
2. **操作步骤**：
   - 读 [prepack.cpp:835-947](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L835-L947)（`alloc_and_load_pack_molecules`）找主循环入口。
   - 跟进 [prepack.cpp:980-1033](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L980-L1033)（`try_create_molecule`），注意 `if (pack_pattern->is_chain)` 分支调用了 `find_new_root_atom_for_chain`。
   - 读 [prepack.cpp:1400-1445](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L1400-L1445)：链头回溯如何沿 `Cin` 驱动方向向上找最上游未占原子。
   - 读 [prepack.cpp:1048-1128](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L1048-L1128)（`try_expand_molecule`）：BFS 如何沿模式连线把 `cout→cin` 上的下一个加法器填进下一个槽位。
3. **需要观察的现象 / 思考题**：假设网表里有一条 5 级加法器链 `a0→a1→a2→a3→a4`，架构 `chain` 模式 `num_blocks` 较大且中段块可选。回答：为什么一次 `try_create_molecule` 不一定把这 5 个全装进一个分子？提示——看「链头回溯 + 每个逻辑块可容纳的位数」与 [prepack.h:60-68](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.h#L60-L68) 关于「超过单个逻辑块最大位数就新开分子」的注释。
4. **预期结果**：你能用一句话讲清进位链绑定的两步机制——**先回溯找链头定根，再 BFS 沿 `cout→cin` 把同链原子逐个填进可选槽，链被切成共享同一 `chain_id` 的多个分子**。
5. **本地验证（可选）**：若已构建，运行时加 `--echo_file on` 并启用 `E_ECHO_PRE_PACKING_MOLECULES_AND_PATTERNS`，可由 [prepack.cpp:941-946](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L941-L946) 的 `print_pack_molecules` 导出每个分子包含的原子与根节点，对照确认链分子内容。**若不确定如何开启 echo，标注「待本地验证」。**

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `compare_pack_pattern` 让块数多的模式优先？
  - **答案**：大模式（如长链、LUT+FF+进位）能容纳的位置少、更难塞进逻辑块，必须先于小模式占用原子；小模式灵活，后处理也能塞。先难后易能提高整体匹配率。
- **练习 2**：`try_expand_molecule` 遇到某槽位填不上时，何时返回 `false`、何时只是跳过？
  - **答案**：若该槽位 `is_block_optional[block_id] == false`（必填，例如非链模式的任一块、或链的根块）→ 返回 `false`，整个分子放弃；若该槽可选（链中段）→ 只跳过，继续 BFS。
- **练习 3**：单原子分子为什么也要建？聚簇器直接处理孤立原子不行吗？
  - **答案**：聚簇器的所有逻辑（种子选择、增益计算、合法性）都以分子为单位。若允许「裸原子」混杂进来，聚簇器就要额外处理「拆分分子」的退化情形，徒增复杂度。把每个孤立原子也包成 `MOLECULE_SINGLE_ATOM` 分子，统一了数据通路（见 [prepack.cpp:904-909](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L904-L909) 注释）。

---

### 4.3 原子与 PB 的双向绑定：expected_lowest_cost_pb_gnode 与 AtomPBBimap

#### 4.3.1 概念说明

分子解决了「哪些原子必须在一起」，但还有一个问题：**每个原子（分子）应该优先放进哪种物理原语（PB）？** 比如 LUT 原子可以进 6-LUT 也可以进 5-LUT，加法器原子要进加法器原语。`Prepacker` 在构建分子的同时，为每个原子预算了一个 **期望最低代价 PB 节点**，作为聚簇时的初始落脚建议。

而一旦聚簇器把原子真正装进某个逻辑块的某个 PB 实例（`t_pb`），就需要一个 **双向映射** 记录「这个原子现在落在哪个 `t_pb`」「这个 `t_pb` 里装的是哪个原子」。这就是 `AtomPBBimap`。

二者一前一后：

- **`expected_lowest_cost_pb_gnode`**（Prepacker，打包前）：原子 → 期望的 PB **类型节点**（`t_pb_graph_node*`，是模板不是实例）。
- **`AtomPBBimap`**（ClusterLegalizer，打包中）：原子 ↔ 实际装入的 PB **实例**（`const t_pb*`，是具体的盒内层次对象）。

#### 4.3.2 核心流程

```text
Prepacker 收尾循环（4.2 已见）：
  对每个原子 blk_id：
    get_expected_lowest_cost_primitive_for_atom_block
      → 在所有逻辑块类型里递归找代价最低、且 primitive_type_feasible 的叶子 PB 节点
    存入 expected_lowest_cost_pb_gnode[blk_id]
        ↓ （聚簇阶段消费）
ClusterLegalizer 装入原子到某 t_pb 实例：
    atom_pb_lookup_.set_atom_pb(blk_id, pb)   // 写双向映射
        ↓
后续阶段（时序分析、图形绘制、网表写出）：
    atom_pb_lookup_.atom_pb(blk_id)           // 原子 → 它在哪个 PB 实例
    atom_pb_lookup_.pb_atom(pb)               // PB 实例 → 它装了哪个原子
```

#### 4.3.3 源码精读

**（a）期望最低代价 PB 节点。** `Prepacker::get_expected_lowest_cost_pb_gnode` 直接返回预算好的节点（[prepack.h:237-243](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.h#L237-L243)）；它由 `get_expected_lowest_cost_primitive_for_atom_block`（[prepack.cpp:806-826](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L806-L826)）在收尾循环里填好：遍历所有逻辑块类型，递归进入 PB 图，在所有 `primitive_type_feasible` 的叶子原语中选 `compute_primitive_base_cost` 最低者。这一步把「架构里有哪些原语能放这个原子」和「哪个最省」提前算好，聚簇器不必每次重新搜。

**（b）AtomPBBimap 双向映射。** 类声明见 [atom_pb_bimap.h:24-57](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/atom_pb_bimap.h#L24-L57)，底层是一个 `vtr::bimap<AtomBlockId, const t_pb*>`（正向用线性容器 `linear_map`、反向用 `unordered_map`，兼顾两个方向的查询效率）。核心接口：

> [atom_pb_bimap.cpp:43-56](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/atom_pb_bimap.cpp#L43-L56) —— `set_atom_pb`：任意一端无效就删映射，两端都有效就更新；`atom_pb`/`pb_atom`（[atom_pb_bimap.cpp:16-32](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/atom_pb_bimap.cpp#L16-L32)）分别按正/反方向查询。

**（c）在 ClusterLegalizer 中的位置。** `ClusterLegalizer` 持有一个 `atom_pb_lookup_` 成员，注释明说它是「全局 `AtomLookup` 中 `AtomPBBimap` 的一份拷贝」（[cluster_legalizer.h:675-677](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.h#L675-L677)），并提供 `const`/`mutable` 双 getter（[cluster_legalizer.h:589-590](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.h#L589-L590)）——这正是 u3-l4 讲过的「生产者取 mutable、消费者取 const」访问模式。每当聚簇器把一个原子钉进某个 `t_pb`，就 `mutable_atom_pb_lookup().set_atom_pb(...)`；簇内布线、时序图构建、图形高亮都通过这份映射在原子层与 PB 实例层之间来回翻译。

#### 4.3.4 代码实践：顺着 get_atom_molecule 与 atom_pb 画一条调用链

1. **实践目标**：把「Prepacker 预算 PB 节点」与「ClusterLegalizer 落地后绑定 PB 实例」两段串成一条完整的原子→PB 路径。
2. **操作步骤**：
   - 在 [prepack.h:223-243](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.h#L223-L243) 找到 `get_atom_molecule` 与 `get_expected_lowest_cost_pb_gnode`，确认它们是 Prepacker 对外的只读查询。
   - 在 [cluster_legalizer.h:589-590](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/cluster_legalizer.h#L589-L590) 找到 `atom_pb_lookup()`/`mutable_atom_pb_lookup()`。
   - 用 `Grep` 在 `vpr/src/pack/cluster_legalizer.cpp` 中搜索 `set_atom_pb`，找到「原子装入 PB」的写点。
3. **需要观察的现象**：`set_atom_pb` 在哪些时机被调用（装入、合法化失败回退时是否清除映射）。
4. **预期结果**：你能写出链路 `AtomBlockId --get_atom_molecule--> PackMoleculeId`（打包前分组）与 `AtomBlockId --set_atom_pb--> t_pb*`（打包中落地），并指出前者是 Prepacker 的产物、后者是 ClusterLegalizer 的产物。
5. 若本地无法运行，明确标注「待本地验证」。

#### 4.3.5 小练习与答案

- **练习 1**：`expected_lowest_cost_pb_gnode` 存的是 `t_pb_graph_node*`（类型模板），`AtomPBBimap` 存的是 `const t_pb*`（实例）。为什么两者类型不同？
  - **答案**：打包前还没有任何 `t_pb` 实例被分配，只能指到 PB 图上的「这种原语节点」（模板）；聚簇时才会按需实例化出 `t_pb`（盒内层次对象），所以落地映射指向实例。模板是「能放哪种」，实例是「放进了哪一个」。
- **练习 2**：`set_atom_pb` 当传入 `pb == nullptr` 时为什么是「删除映射」而不是报错？
  - **答案**：聚簇合法化可能回退——把原子从某 `t_pb` 拿出来重试。用 `nullptr` 表示「解除绑定」是常规操作，不是错误，所以设计成 erase 语义。

---

## 5. 综合实践

把本讲三个模块串起来，做一次「**从 XML 标注到分子内存结构**」的端到端阅读。

任务：

1. 选定进位链架构 [vtr_flow/arch/xilinx/7series_BRAM_DSP_carry.xml](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/arch/xilinx/7series_BRAM_DSP_carry.xml) 的 `name="chain"` 这一组 `<pack_pattern>`（[行 1418-1432](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vtr_flow/arch/xilinx/7series_BRAM_DSP_carry.xml#L1418-L1432)）。
2. 写出它经历的三次形态变化，并各给一个源码位置：
   - **XML 标注** →（u2 解析后挂在 PB 图边上）→
   - **`t_pack_patterns`**（`is_chain=true`，根块=`fle[0]`，中段块可选）：见 [prepack.cpp:153-219](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L153-L219) 与 [pack_patterns.h:94-117](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/pack_patterns.h#L94-L117)。
   - **`t_pack_molecule`**（一串加法器原子填进可选槽，`chain_id` 标识同一条链）：见 [prepack.cpp:980-1033](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.cpp#L980-L1033) 与 [prepack.h:69-114](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.h#L69-L114)。
3. 用一段话回答：**如果架构里完全没有 `<pack_pattern>` 标注，`Prepacker` 还会工作吗？产出的分子长什么样？**
   - 参考答案：仍会工作。`alloc_and_load_pack_patterns` 返回空列表，主循环不生成任何强制打包分子；收尾循环把每个原子各包成一个 `MOLECULE_SINGLE_ATOM` 分子（`base_gain=1`、`chain_id=INVALID`）。聚簇器退化为按原子逐个装箱，失去进位链/强制对的打包质量加成，但流程完整可用。这正体现了「架构驱动」——架构不给的提示，算法按最保守策略兜底。

> 若你想真的跑起来看分子，最直接的验证路径见 4.2.4 第 5 步的 echo 输出；不确定如何开启就写「待本地验证」，不要假装跑过。

## 6. 本讲小结

- **pack pattern 是架构概念**：作者在 `<interconnect>` 的 `<direct>` 边上用 `<pack_pattern name=...>` 标注「值得保持相邻的连线」，典型用途是 LUT→FF 强制对与进位链。
- **识别三步**：`alloc_and_load_pack_patterns` 在 PB 图上发现模式名 → 建空 `t_pack_patterns` → 从扩展边前后扩展出模式的块与连线，途中若模式连到根块输入则置 `is_chain=true`、链中段块置可选。
- **分子是聚簇的基本单元**：`Prepacker` 保证「每个原子恰好属于一个分子」。匹配上模式的原子组成 `MOLECULE_FORCED_PACK` 分子，其余兜底成 `MOLECULE_SINGLE_ATOM`。
- **进位链绑定 = 链头回溯 + BFS 填槽**：`find_new_root_atom_for_chain` 沿 `Cin` 向上找最上游未占原子定根，`try_expand_molecule` 再沿 `cout→cin` 把同链原子填进可选槽；长链被切成共享同一 `chain_id` 的多个分子，供布局摆在相邻逻辑块。
- **两类 PB 绑定**：打包前 Prepacker 预算 `expected_lowest_cost_pb_gnode`（原子→PB 模板）；打包中 ClusterLegalizer 用 `AtomPBBimap` 维护原子↔`t_pb` 实例的双向映射，供时序、图形等下游使用。
- **简化假设**：强制打包网默认单扇出；模式按「大优先、低代价优先」first-fit 占用原子。

## 7. 下一步学习建议

- 下一讲 **u4-l3 贪心聚簇器 GreedyClusterer**：分子已经就绪，聚簇器如何用 `base_gain` 选种子、如何用 `get_expected_lowest_cost_pb_gnode` 给分子找落脚点、吸引力组（attraction groups）如何进一步影响选择。本讲的 `Prepacker::get_atom_molecule` 与 `get_molecule_root_atom` 是聚簇器的直接输入。
- 之后 **u4-l4 聚簇合法化与簇内布线**：分子装进簇后，由 `lb_type_rr_graph` 做簇内布线合法性判定；本讲的 `AtomPBBimap` 在那里被密集读写。
- 延伸阅读：[prepack.h:153-179](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/pack/prepack.h#L153-L179) 的类注释给出了进位链分子的经典描述；想理解长链对布局的约束，可继续看 `t_chain_info::is_long_chain` 的消费者（布局阶段）。
