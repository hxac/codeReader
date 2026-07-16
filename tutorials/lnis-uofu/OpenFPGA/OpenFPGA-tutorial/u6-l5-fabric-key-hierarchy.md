# Fabric Key 与 Fabric 层级输出

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 **fabric key**（fabric 钥匙）到底是什么：它不是加密密钥，而是「可配置存储器的布局清单」——描述成千上万个配置位实例在芯片里如何被排序与寻址。
- 区分产生 fabric key 的**两条路径**：`build_fabric` 的 `--write_fabric_key / --load_fabric_key / --generate_random_fabric_key` 选项，与独立的 `write_fabric_key` 命令。
- 理解 `write_fabric_hierarchy` 输出的纯文本层级树如何服务于**层次化后端（hierarchical PnR）**流程。
- 认识 `io_location_map`、`fabric_global_port_info`、`module_name_map` 这三类「不直接是网表、却贯穿后续比特流与 testbench 生成」的附属产物。

本讲是 u6（Fabric 构建）的收尾：u6-l4 讲完了顶层模块与配置总线在内存里怎么搭起来，本讲则回答「搭好之后，OpenFPGA 还会把 fabric 的哪些元信息导出/缓存出来，给下游谁用」。

## 2. 前置知识

- **可配置子模块（configurable children）与配置区域（config regions）**：见 u6-l4。顶层模块 `fpga_top` 把所有承载配置位的子模块（物理存储器）排成一张蛇形列表，并均匀切成若干「配置区域」。本讲反复出现的 `configurable_children(top_module, PHYSICAL)` 就是这张列表。
- **配置协议（configuration protocol）**：见 u3-l4。`scan_chain` 串行、`memory_bank` 矩阵寻址（BL/WL）、`frame_based` 帧寻址、`ql_memory_bank` 可挂移位寄存器 bank。配置协议决定了配置位的寻址方式，也就决定了 fabric key 里要记录哪些额外信息。
- **ModuleManager 的 SoA + 强类型 ID 风格**：见 u6-l1。本讲的 FabricKey、FabricGlobalPortInfo 等数据结构沿用同一风格（每条属性一条 `vtr::vector`，按下标对齐）。
- **annotation 模式**：见 u5-l4。本讲的 `io_location_map`、`fabric_global_port_info` 都是 OpenFPGA 自有、挂在 `OpenfpgaContext` 里的「副表」。
- **常量执行函数 vs 普通执行函数**：见 u2-l2。`set_command_const_execute_function` 表示该命令**只读** context（不改 fabric），本讲的三个写命令全部是只读的。

> 关键直觉：u6-l2~u6-l4 产出的是「网表/比特流的素材」，本讲产出的是「fabric 的说明书」——告诉下游工具（PnR、比特流烧写器、testbench 生成器）这片 fabric 长什么样、IO 在哪、全局端口怎么驱动。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [openfpga/src/fabric/fabric_key_writer.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_key_writer.cpp) | 遍历顶层模块的可配置子模块，组装 `FabricKey` 对象并写成 `fabric_key.xml`。 |
| [openfpga/src/fabric/fabric_hierarchy_writer.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_hierarchy_writer.cpp) | 用 DFS 遍历模块图，输出带缩进的纯文本层级树 `fabric_hierarchy.txt`。 |
| [openfpga/src/fabric/build_fabric_io_location_map.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_io_location_map.cpp) | 建立「顶层 GPIO 端口索引 ↔ (x,y,z) 坐标」的快速查找表 `IoLocationMap`。 |
| [openfpga/src/fabric/fabric_global_port_info.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_global_port_info.cpp) | `FabricGlobalPortInfo` 数据结构的成员实现（全局端口属性账本）。 |
| [openfpga/src/fabric/build_fabric_global_port_info.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_global_port_info.cpp) | 从 `circuit_library` 与 `tile_annotation` 收集全局端口，填充 `FabricGlobalPortInfo`。 |
| [libs/libfabrickey/src/base/fabric_key.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libfabrickey/src/base/fabric_key.h) | `FabricKey` 内存数据结构（region → key → BL/WL bank → module subkey）。 |
| [libs/libfabrickey/src/io/fabric_key_xml_constants.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libfabrickey/src/io/fabric_key_xml_constants.h) | `fabric_key.xml` 的标签/属性字符串常量。 |
| [openfpga/src/base/openfpga_build_fabric_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h) | 命令执行模板：在 `build_fabric` 末尾构建 io/global 信息，以及三个 `write_*_template` 函数。 |
| [openfpga/src/base/openfpga_setup_command_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h) | 三个写命令 + `build_fabric` 的 fabric-key 选项的注册。 |

## 4. 核心概念与源码讲解

### 4.1 Fabric Key 写入

#### 4.1.1 概念说明

**Fabric key 是什么？** 一片 FPGA 里有成千上万个配置位（config bit），它们被组织成一个个「可配置子模块」（物理存储器实例）。`build_fabric` 决定了这些实例在顶层模块下的**排列顺序**——而这个顺序就是「地址」。换句话说：

- 改变可配置子模块的排列顺序，就改变了每个配置位被寻址到的地址；
- 同一份电路结构 + 不同的排列 = 比特流里每一位的「地址」完全不同。

**fabric key 就是这份「排列清单」的持久化记录**：它把顶层模块下每个配置位实例的 `name`（模块名）、`value`（实例号）、`alias`（可选的实例名）、坐标，以及它属于哪个配置区域，原原本本写进 `fabric_key.xml`。

为什么要单独导出它？有三类用途：

1. **可复现（reproducibility）**：把上一次构建的 `fabric_key.xml` 用 `--load_fabric_key` 重新喂回去，OpenFPGA 就会按这份既有顺序排布存储器，得到**地址布局完全一致**的 fabric。
2. **安全 / 混淆（`--generate_random_fabric_key`）**：随机打乱存储器地址。这样即使两片芯片电路完全相同，比特流里每一位对应的物理地址也不同，起到类似「混淆/加密」的作用，增加逆向抄板的成本。
3. **模块级 subkey（`--include_module_keys`）**：除了顶层 key，还可以为每个含可配置子模块的中间模块单独导出一份子钥匙，支持更细粒度的可寻址性。

> 注意术语辨析：fabric key 里的 "key" 是「钥匙/索引」，不是密码学意义上的「密钥」。它的本质是一张**地址编排表**。

#### 4.1.2 核心流程

产生 fabric key 有两条路径，但底层都汇入同一个函数 `write_fabric_key_to_xml_file()`：

```text
路径 A：build_fabric 选项
  build_fabric --write_fabric_key ./fabric_key.xml          # 导出当前 fabric 的 key
              --load_fabric_key  external_key.xml           # 按既有 key 重建（复现/安全）
              --generate_random_fabric_key                  # 随机打乱地址

路径 B：独立命令（build_fabric 之后任意时刻调用）
  write_fabric_key -f ./fabric_key.xml --include_module_keys

            │
            ▼
  write_fabric_key_to_xml_file(module_manager, fname, config_protocol,
                               blwl_sr_banks, include_module_keys, verbose)
            │
            ├─ 1. 定位顶层模块（优先 fpga_core，否则 fpga_top）
            ├─ 2. 建 region_id_map：ConfigRegionId ↔ FabricRegionId 一一对应
            ├─ 3. 逐区域遍历 region_configurable_children：
            │      - 跳过协议专属子模块（如译码器，estimate_..._to_skip）
            │      - 对每个子模块 create_key + set name/value/alias/coordinate
            │      - add_key_to_region
            ├─ 4. 若有 BL/WL 移位寄存器 bank：写入 bl/wl_shift_register_banks
            ├─ 5. 若 --include_module_keys：为每个中间模块建 module subkey
            └─ 6. write_xml_fabric_key()（libfabrickey 负责 XML 落盘）
```

「跳过译码器」这一步很关键：`memory_bank` / `frame_based` 协议下，顶层会挂一些**译码器子模块**，它们本身不存用户配置位、只是寻址电路。这些不该出现在 key 里，所以用 `estimate_num_configurable_children_to_skip_by_config_protocol()` 把它们从计数里扣掉。

#### 4.1.3 源码精读

**函数入口与顶层模块定位**（同时支持 `fpga_core` 与 `fpga_top`，两者都在时优先用 core）：

[openfpga/src/fabric/fabric_key_writer.cpp:L101-L116](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_key_writer.cpp#L101-L116) — 先找 `fpga_top`，再找 `fpga_core`；若两者皆有效，把 `top_module` 指向 core。这对应 OpenFPGA 的「fpga_core / fpga_top 双顶层」设计：core 是真正含逻辑的内核，top 是包了 IO ring 的外壳。

**逐区域生成 key**（核心循环，下一段做了精简标注）：

[openfpga/src/fabric/fabric_key_writer.cpp:L140-L187](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_key_writer.cpp#L140-L187) — 对每个 `ConfigRegionId`：先查 `region_id_map` 拿到对应的 `FabricRegionId`；用 `estimate_num_configurable_children_to_skip_by_config_protocol` 算出要跳过的译码器数量；然后遍历保留下的子模块，对每一个 `create_key()` 并写入四元属性：

```cpp
FabricKeyId key = fabric_key.create_key();
fabric_key.set_key_name(key, module_manager.module_name(child_module));      // 模块名
fabric_key.set_key_value(key, child_instance);                              // 实例号
if (false == module_manager.instance_name(...).empty()) {
  fabric_key.set_key_alias(key, module_manager.instance_name(...));         // 可选别名
}
fabric_key.set_key_coordinate(key, child_coord);                            // (x,y) 坐标
fabric_key.add_key_to_region(fabric_region, key);
```

**跳过译码器**：

[openfpga/src/fabric/fabric_key_writer.cpp:L149-L155](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_key_writer.cpp#L149-L155) — `num_child_to_skip` 由配置协议决定，从 `curr_region_num_config_child` 里减掉，这样 key 里只保留真正承载用户配置位的存储器实例。

**BL/WL 移位寄存器 bank**（仅 `ql_memory_bank` + `shift_register` 子协议才非空）：

[openfpga/src/fabric/fabric_key_writer.cpp:L190-L229](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_key_writer.cpp#L190-L229) — 当 `blwl_sr_banks.regions()` 非空时，把每个区域的 BL bank、WL bank 及其驱动的 data port 写进 key。这是 u9-l1（移位寄存器 bank）与本讲的衔接点：fabric key 必须记录「哪个移位寄存器 bank 驱动哪些 BL/WL 端口」，否则下次复现时移位寄存器组织就对不上。

**模块级 subkey**（`--include_module_keys` 才走）：

[openfpga/src/fabric/fabric_key_writer.cpp:L25-L71](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_key_writer.cpp#L25-L71) — 跳过 `fpga_top`/`fpga_core`，跳过没有任何可配置子模块的模块；对其余模块 `create_module(module_name)`，再为它的每个物理可配置子模块 `create_module_key()` 写 name/value/alias。

**内存数据结构 `FabricKey`**（libfabrickey 库，SoA 风格）：

[libs/libfabrickey/src/base/fabric_key.h:L41-L100](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libfabrickey/src/base/fabric_key.h#L41-L100) — 头注释写得很清楚：「A fabric may consist of multiple regions, each region contains a number of keys, each key can only be defined in one unique region」。它把 region、key、BL/WL bank、module subkey 四类对象用强类型 ID 组织起来。

**XML 标签常量**（决定 `fabric_key.xml` 长什么样）：

[libs/libfabrickey/src/io/fabric_key_xml_constants.h:L7-L28](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libfabrickey/src/io/fabric_key_xml_constants.h#L7-L28) — 根节点 `fabric_key`，下含 `region`（属性 `id`）、`key`（属性 `id / alias / name / value / column / row`）、`module`、以及 `bl_shift_register_banks` / `wl_shift_register_banks`（每个 `bank` 带 `id` 与 `range`）。所以一份典型 `fabric_key.xml` 形如：

```xml
<!-- 示例：fabric_key.xml 的结构（依据 XML 常量推断，具体值待本地验证） -->
<fabric_key>
  <region id="0">
    <key id="0" name="mem_..." value="0" alias="grid_io_top_left.mem_0" column="0" row="3"/>
    <key id="1" name="mem_..." value="1" alias="..." column="0" row="3"/>
    ...
    <bl_shift_register_banks>
      <bank id="0" range="mem_bl[0:127]"/>
    </bl_shift_register_banks>
  </region>
  ...
  <module name="grid_clb">
    <key name="mem_..." value="0" alias="..."/>
  </module>
</fabric_key>
```

**两条路径的注册**：

- 路径 A（build_fabric 选项）：[openfpga/src/base/openfpga_setup_command_template.h:L426-L457](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L426-L457) — 注册 `--load_fabric_key`、`--write_fabric_key`、`--generate_random_fabric_key`。
- 路径 B（独立命令）：[openfpga/src/base/openfpga_setup_command_template.h:L1016-L1042](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L1016-L1042) — `write_fabric_key -f <xml> [--include_module_keys] [--verbose]`，声明依赖 `build_fabric`。
- 路径 A 在 `build_fabric` 末尾就地调用：[openfpga/src/base/openfpga_build_fabric_template.h:L255-L269](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L255-L269)；路径 B 的模板：[openfpga/src/base/openfpga_build_fabric_template.h:L278-L298](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L278-L298)。两者最终都调用同一个 `write_fabric_key_to_xml_file()`。

#### 4.1.4 代码实践

> **实践目标**：亲手生成一份 `fabric_key.xml`，对照源码确认它的 region/key/bank 结构来自哪里。

**操作步骤**：

1. `source openfpga.sh`（在仓库根目录），确认 `OPENFPGA_PATH` 已设置。
2. 用专门生成 key 的脚本跑一个最小任务（注意：`generate_fabric_example_script.openfpga` **不**写 fabric key，要改用 `generate_fabric_key_example_script.openfpga`，它在 `build_fabric` 上带了 `--write_fabric_key ./fabric_key.xml`）：

   ```bash
   run-task basic_tests/generate_fabric/testconfig_generation_fabric_key
   ```

   > 任务路径以仓库实际目录为准（待本地确认 `openfpga_flow/tasks` 下对应的 generate_fabric_key 任务名）。
3. 用 `goto-task` 进入最新 run 目录，打开产物里的 `fabric_key.xml`。

**需要观察的现象**：

- 文件根标签是 `<fabric_key>`，下含若干 `<region id="...">`，每个 region 内是一长串 `<key .../>`。
- 对照 [fabric_key_writer.cpp:L159-L186](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_key_writer.cpp#L159-L186)：每个 key 的 `name` 来自 `module_name(child_module)`、`value` 来自 `child_instance`、`column/row` 来自 `child_coord`。
- 如果用的是 `cc`（scan_chain）协议，文件里**不会**出现 `bl_shift_register_banks`/`wl_shift_register_banks`（因 `blwl_sr_banks.regions()` 为空，对应 [L190](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_key_writer.cpp#L190) 的 `if` 不进入）；换成 `qlbanksr` 协议才会出现。

**预期结果**：得到一份合法的 `fabric_key.xml`，region 数 = 配置区域数，key 总数 ≈ 顶层物理可配置子模块数 − 译码器数。

> 若无法实际运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `add_module_keys_to_fabric_key` 要跳过 `fpga_top` / `fpga_core` 这两个模块？
**答案**：fabric key 的「主键」部分已经由顶层逐区域枚举了顶层模块的所有可配置子模块（见 L140-L187）。如果再为 `fpga_top`/`fpga_core` 自身建一份 module subkey，就会和主键重复。module subkey 只服务于**中间层**模块（如某个 grid/tile 内部还有自己的可配置子模块），所以必须跳过最顶层。

**练习 2**：`--generate_random_fabric_key` 打乱了什么？打乱后比特流还能烧进同一片芯片吗？
**答案**：它打乱的是可配置存储器实例的**地址编排顺序**，即 fabric key 里每个 key 的 `value`/排序。电路结构本身不变，所以比特流仍可烧进同一片芯片——但因为地址映射变了，必须用**与本次 key 配套生成**的比特流，否则位会写错地址。这正是「安全/混淆」的要点：抄走网表但拿不到 key，比特流地址就对不上。

**练习 3**：`estimate_num_configurable_children_to_skip_by_config_protocol` 大致会跳过哪类子模块？
**答案**：跳过配置协议专属的**译码器**子模块（如 `memory_bank`/`frame_based` 顶层挂的 BL/WL 译码器或帧译码器）。它们是寻址电路、不承载用户配置位，不应计入 key。

---

### 4.2 Fabric Hierarchy（层级树）写入

#### 4.2.1 概念说明

`write_fabric_hierarchy` 把 ModuleManager 里的模块嵌套关系，导出成一份**带缩进的纯文本树**。例如：

```text
fpga_top:
  grid_io_top:
    - ...
  sb_0__1_:
    mem_...:
      - ...
  cbx_1__0_:
    - ...
```

它服务的场景是 **层次化布局布线（hierarchical PnR）**：当芯片大到 flat（扁平）PnR 跑不动时，后端工具希望保留 fabric 的模块层级，按层次分块做 PnR。这份文本树就是告诉后端工具「fabric 在模块层面长什么样、谁是谁的子模块」。

它本身**不生成网表**（网表是 u8-l1 的 `write_fabric_verilog` 的职责），只是网表模块层级的一份「目录」。

#### 4.2.2 核心流程

```text
write_fabric_hierarchy -f out.txt [--module pat] [--filter pat] [--depth N] [--exclude_empty_modules]
        │
        ▼
write_fabric_hierarchy_to_text_file(module_manager, module_name_map, fname,
                                    root_module_names, module_name_filter,
                                    hie_depth_to_stop, exclude_empty_modules)
        │
        ├─ 1. 遍历所有模块，用通配符（*→.*，?→.）匹配「根模块名」
        │      （默认根 = module_name_map 映射后的 fpga_top）
        ├─ 2. 对每个匹配的根模块：
        │      - 输出 "root_name:\n"
        │      - 若 --exclude_empty_modules 且该模块无合格子模块 → 跳过
        │      - 从 depth=1 起 DFS 递归
        └─ 3. rec_output_module_hierarchy_to_text_file（DFS）：
               - 若当前深度 > hie_depth_to_stop → 停止
               - 对每个子模块：通配符过滤；不匹配则 continue
               - 缩进 = depth * 2 个空格
               - 叶子或到达目标深度 → "- name\n"（列表风格）
                 否则 → "name:\n"（树风格，继续往下挖）
```

这里有两套通配符：`--module` 选**根**（默认 `fpga_top`），`--filter` 选**每个根下的子树里允许出现哪些模块名**（默认 `*`，即全部）。两者都支持把 `*` 转成正则 `.*`、`?` 转成 `.` 来匹配。

#### 4.2.3 源码精读

**入口与根模块匹配**：

[openfpga/src/fabric/fabric_hierarchy_writer.cpp:L181-L219](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_hierarchy_writer.cpp#L181-L219) — 遍历所有模块，把模块名（先经 `module_name_map` 翻译）与 `root_module_names` 通配符做 `std::regex_match`；命中则作为根，输出 `curr_module_name << ":\n"`，再从 depth=1 起 DFS。`exclude_empty_modules` 会在 DFS 前先用 `module_filter_all_children` 判断该模块是否「没有合格子模块」，是则跳过。

**DFS 递归本体**：

[openfpga/src/fabric/fabric_hierarchy_writer.cpp:L51-L136](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_hierarchy_writer.cpp#L51-L136) — 关键三段：

- **深度截断**（[L57-L59](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_hierarchy_writer.cpp#L57-L59)）：`hie_depth_to_stop < current_hie_depth` 即返回，控制树有多深。
- **列表 vs 树的判定**（[L67-L75](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_hierarchy_writer.cpp#L67-L75) 与 [L119-L123](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_hierarchy_writer.cpp#L119-L123)）：若一个模块的所有子模块都已是「叶子」（没有合格孙模块），就用 `- name` 的列表写法，更紧凑；否则用 `name:` 的树写法继续展开。
- **缩进**（[L116-L118](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_hierarchy_writer.cpp#L116-L118)）：`write_space_to_file(fp, current_hie_depth * 2)`，每深一层多缩进 2 个空格。

**module_name_map 的介入**：注意 [L80-L83](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_hierarchy_writer.cpp#L80-L83) 与 [L101-L104](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_hierarchy_writer.cpp#L101-L104)：输出前都先查 `module_name_map`，若该模块名有「对外别名」就用别名。这正是 `rename_modules` 命令（[openfpga_setup_command_template.h:L1049-L1070](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L1049-L1070)）与本节的衔接：用户可以用 `rename_modules` 把内部名（如 `cbx_1__0_`）改成自定义名，hierarchy 输出会跟着变。这也是 OpenFPGA 支持 `fpga_core` wrapper 自定义命名的基础（详见 4.4）。

**命令注册**：

[openfpga/src/base/openfpga_setup_command_template.h:L480-L529](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L480-L529) — 选项 `--file/-f`（必需）、`--module`（根名通配符，默认 fpga_top）、`--filter`（子模块名通配符，默认 `*`）、`--depth`（停止深度）、`--exclude_empty_modules`、`--verbose`；绑定 **const** 执行函数 `write_fabric_hierarchy_template`（只读 context，不修改 fabric）。

#### 4.2.4 代码实践

> **实践目标**：生成 `fabric_hierarchy.txt`，并通过 `--depth` / `--filter` 观察树的裁剪效果。

**操作步骤**：

1. 在交互式 shell 里先跑通最小 fabric（以下命令在 `openfpga` 交互模式中逐行输入，或写进一个 `.openfpga` 脚本用 `-f` 执行）：

   ```text
   vpr ${VPR_ARCH_FILE} ${VPR_TESTBENCH_BLIF} --clock_modeling route
   read_openfpga_arch -f ${OPENFPGA_ARCH_FILE}
   link_openfpga_arch --activity_file ${ACTIVITY_FILE} --sort_gsb_chan_node_in_edges
   build_fabric --compress_routing
   write_fabric_hierarchy --file ./fabric_hierarchy.txt
   exit
   ```

   > 更省事的做法是直接用现成脚本：`run-task` 跑 `generate_fabric_example_script.openfpga` 对应的任务（该脚本第 25 行正是 `write_fabric_hierarchy --file ./fabric_hierarchy.txt`）。
2. 打开产物 `fabric_hierarchy.txt`，确认根是 `fpga_top:`，下缩进 2 空格列出 `grid_io_*`、`sb_*`、`cbx_*`/`cby_*`、`grid_clb` 等。
3. 重跑并加 `--depth 1`，再重跑并加 `--filter "sb_*"`，对比三次输出差异。

**需要观察的现象**：

- `--depth 1`：只列根的直接子模块，每个写成 `- name`（因到达目标深度，走列表分支）。
- `--filter "sb_*"`：根的直接子模块里只有名字匹配 `sb_*` 的留下，其余被 `continue` 跳过（对照 [L112-L114](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_hierarchy_writer.cpp#L112-L114)）。

**预期结果**：能直观看到「列表风格」与「树风格」两种输出形态，并理解 `--depth`/`--filter` 如何裁剪层级树。

> 若无法实际运行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么同一份 hierarchy 文件里，有的行是 `name:` 有的行是 `- name`？
**答案**：见 [L119-L123](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_hierarchy_writer.cpp#L119-L123)。当某个模块的所有子模块都已是叶子（无合格孙模块），或当前深度已达 `hie_depth_to_stop`，就用 `- name` 列表写法（紧凑、表示「到此为止」）；否则用 `name:` 树写法（表示「下面还要展开」）。

**练习 2**：`--module` 与 `--filter` 各自匹配什么？
**答案**：`--module` 匹配**根模块名**（决定从哪些模块开始画树，默认 `fpga_top`）；`--filter` 匹配**每棵子树里允许出现的子模块名**（默认 `*` 全部）。两者都把 `*`→`.*`、`?`→`.` 转成正则。

**练习 3**：hierarchy 输出对「层次化 PnR」为什么重要？
**答案**：层次化 PnR 需要保留模块边界、按层次分块做布局布线。这份纯文本树告诉后端工具 fabric 的模块嵌套关系（谁包含谁），使后端可以按模块粒度分配 PnR 任务、复用模块级结果，从而处理 flat PnR 跑不动的大阵列。

---

### 4.3 IO Location Map（IO 位置映射）

#### 4.3.1 概念说明

`IoLocationMap` 回答一个问题：**顶层模块 `fpga_top` 的某个 GPIO 端口的第 N 根 pin，物理上连到了芯片哪个 (x, y, z) 坐标的 IO subtile？**

这看似简单，却是 testbench 生成与引脚约束（PCF）的关键查找表：

- **testbench 生成**（u8-l2）：仿真时要给某个 IO 加激励或采样，必须知道它在顶层端口上的 pin 序号。
- **PCF / pin constraint**（u10-l3）：用户在 `.pcf` 里写「逻辑端口 a → 物理引脚」，需要把物理引脚坐标翻译成顶层端口的 pin 索引。
- **vpr_bitstream_annotation**：build_fabric 末尾就用 io_location_map 把 PCF 定义的引脚坐标回填给 bitstream 标注（见下方源码）。

#### 4.3.2 核心流程

io_location_map 不是靠某个独立命令「构建」的——它在 **`build_fabric` 内部**就建好并存进 context：

```text
build_fabric
   └─ (末尾) build_fabric_io_location_map(module_graph, grids, tiled_fabric)
         ├─ tiled_fabric == true  → build_fabric_tiled_io_location_map(...)
         └─ false                 → build_fabric_fine_grained_io_location_map(...)
                │
                ├─ 遍历 top_module 的 io_children 列表（IO 顺序在此已定）
                ├─ 对每个 IO grid 的每个 subtile：
                │     - 跳过 EMPTY、跳过 width/height>1 的异构块
                │     - 找出 subchild 的「可映射 IO 端口」（port_is_mappable_io）
                │     - 按端口名维护递增计数器 io_counter
                │     - io_location_map.set_io_index(x, y, z, port_name, index)
                └─ 校验：顶层每个 GPIO 端口的 pin 数 == 已映射数量
   └─ 存入 openfpga_ctx.mutable_io_location_map()

# 之后用独立命令导出成 XML：
write_fabric_io_info -f ./fabric_io_location.xml [--no_time_stamp] [--verbose]
   └─ openfpga_ctx.io_location_map().write_to_xml_file(...)
```

注意分两个变体：**fine-grained**（普通 grid 直接挂在顶层）与 **tiled**（启用了 `--group_tile`，IO 在 tile 模块里多嵌一层）。两者的差别仅在「多套一层子模块」的遍历深度，逻辑一致。

#### 4.3.3 源码精读

**分派入口**：

[openfpga/src/fabric/build_fabric_io_location_map.cpp:L283-L290](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_io_location_map.cpp#L283-L290) — 根据 `tiled_fabric`（即 `build_fabric` 是否带 `--group_tile`）二选一。文件顶部 FIXME 注释坦言两种 fabric 的访问方式还不统一，待后续重构。

**fine-grained 变体的核心遍历**：

[openfpga/src/fabric/build_fabric_io_location_map.cpp:L48-L126](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_io_location_map.cpp#L48-L126) — 关键点：

- 注释（[L30-L33](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_io_location_map.cpp#L30-L33)）明确：「I/O sequence(indexing) is already determined in the `io_children()` list of top-level module」——IO 的顺序早在 build_top_module 排 `io_children` 时就定好了，本函数只是建一张「坐标 → 索引」的快速反查表。
- 跳过 EMPTY 与异构块（[L59-L67](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_io_location_map.cpp#L59-L67)）。
- 三层嵌套：`io_children(top)` → 每个 IO grid 的 `io_children(child)`（subtile）→ subtile 的 GPIO 端口（[L85-L124](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_io_location_map.cpp#L85-L124)）。注释（[L87-L96](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_io_location_map.cpp#L87-L96)）解释了为什么要下钻到 subchild：grid 级 IO 模块含全部 GPIO，而单个 subtile 可能只占其中一部分。
- `set_io_index(x, y, z=subchild_coord.x, port_name, io_counter[port_name])`，端口计数器按端口名各自从 0 递增。

**完整性校验**：

[openfpga/src/fabric/build_fabric_io_location_map.cpp:L128-L144](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_io_location_map.cpp#L128-L144) — 遍历顶层所有可映射 IO 端口，断言 `io_counter[port] == port.get_width()`，确保每个 GPIO pin 都被映射到、无遗漏。

**在 build_fabric 末尾存入 context + 回填 bitstream 标注**：

[openfpga/src/base/openfpga_build_fabric_template.h:L225-L246](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L225-L246) — `mutable_io_location_map() = build_fabric_io_location_map(...)`；紧接着遍历 `vpr_bitstream_annotation` 里 PCF 定义的引脚，用 io_location_map 把 `(x,y,z)` 坐标查出来回填。若 PCF 引脚坐标查不到（`!pin_valid`）即返回 `CMD_EXEC_FATAL_ERROR`——这就是「PCF 引脚写错坐标」时报错的来源。

**独立写命令**：

- 注册：[openfpga/src/base/openfpga_setup_command_template.h:L537-L569](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L537-L569) — `write_fabric_io_info -f <xml> [--no_time_stamp] [--verbose]`，依赖 `build_fabric`。
- 模板：[openfpga/src/base/openfpga_build_fabric_template.h:L358-L377](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L358-L377) — 直接调 `openfpga_ctx.io_location_map().write_to_xml_file(...)`，即把已建好的映射落盘成 `fabric_io_location.xml`。

#### 4.3.4 代码实践

> **实践目标**：导出 `fabric_io_location.xml`，理解其中每条记录对应源码里的哪次 `set_io_index`。

**操作步骤**：

1. 用 `generate_fabric_example_script.openfpga`（其第 29 行就是 `write_fabric_io_info --file ./fabric_io_location.xml --verbose`）跑一个任务：

   ```bash
   run-task basic_tests/generate_fabric/generate_fabric
   ```
   > 任务名以仓库实际目录为准（待本地确认）。
2. 打开产物 `fabric_io_location.xml`，挑一条记录看它的 `x/y/z` 与 `index`。

**需要观察的现象**：文件记录了顶层 GPIO 端口的每一根 pin 与 `(x, y, z)` 坐标的对应关系。对照 [build_fabric_io_location_map.cpp:L119-L122](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_io_location_map.cpp#L119-L122) 即可解释 `index` 的来源（按端口名从 0 递增的计数器）。

**预期结果**：能讲清楚「为什么 testbench 与 PCF 都依赖这张表」——因为它们都需要在「逻辑端口 pin」与「物理 IO 坐标」之间互译。

> 若无法实际运行，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 IO 的索引顺序不是在本函数里决定的？
**答案**：见 [L30-L33](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_io_location_map.cpp#L30-L33) 注释。IO 的排列顺序在 `build_top_module` 排布 `io_children` 列表时就定好了；本函数只是沿这张既定列表遍历，建一张「坐标 → pin 索引」的反查表。

**练习 2**：fine-grained 与 tiled 两个变体的本质区别是什么？
**答案**：是否启用了 `--group_tile`。tiled fabric 把 grid+sb+cb 打包进 tile 模块，IO 子模块比 fine-grained 多嵌一层（要 `io_children(child)` 再下钻到 tile_child 再到 subchild），所以遍历深度不同；见 [L199-L253](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_io_location_map.cpp#L199-L253)。

**练习 3**：如果 PCF 里写了一个不存在的引脚坐标，会在哪里、以什么形式报错？
**答案**：在 [openfpga_build_fabric_template.h:L238-L243](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L238-L243)：用 io_location_map 查坐标得到 `!pin_valid`，打印 `Pin %s defined in pcf command is invalid!` 并返回 `CMD_EXEC_FATAL_ERROR`。

---

### 4.4 Fabric Global Port Info 与 module_name_map

#### 4.4.1 概念说明

**FabricGlobalPortInfo** 是 fabric 所有「全局端口」的属性账本。全局端口是那些扇布整个芯片的特殊信号：时钟（clock）、置位（set）、复位（reset）、编程使能（prog）、配置使能（config_enable）、移位寄存器时钟（shift_register）、BL/WL 时钟、IO 等，外加每个端口的默认值（default value）。

它的用途很集中——**testbench 生成**（u8-l2）要用它来正确驱动这些端口：仿真前要知道哪个端口是编程时钟、哪个是复位、复位默认值是 0 还是 1，否则 testbench 行为会错。fabric_global_port_info 也被 `write_fabric_bitstream` 等命令读取（例如 fast_configuration 要靠全局 set/reset 端口决定跳哪些位）。

与前三类产物最大的不同：**FabricGlobalPortInfo 没有独立的「写文件」命令**。它在 `build_fabric` 内部建好、存进 context，仅供后续命令在内存里读取。它不出现在产物目录里。

**module_name_map** 则是另一类「附属账本」：记录「内部模块名 → 对外自定义名」的映射。它由 `rename_modules` 命令填充，被 `write_fabric_hierarchy`、`write_fabric_verilog` 等输出环节消费（见 4.2.3 已述）。它是 OpenFPGA 支持 `fpga_core` wrapper 自定义命名的底层机制——用户可以给模块起对外友好的名字，而不影响内部 ModuleManager 的标识。

#### 4.4.2 核心流程

```text
build_fabric（末尾）
   └─ build_fabric_global_port_info(module_graph, config_protocol,
                                    tile_annotation, circuit_lib)
         ├─ 来源 1：circuit_library 的全局端口（find_circuit_library_global_ports）
         │     - 在顶层模块找同名端口，找不到则跳过
         │     - 逐属性填：is_clock / is_set / is_reset / is_prog /
         │                 is_shift_register / is_config_enable / default_value
         │     - BL/WL 特判：若端口属于 BL/WL 移位寄存器模型 → 置 is_bl / is_wl
         ├─ 来源 2：tile_annotation 的全局端口（去重后同样填属性）
         └─ 返回 FabricGlobalPortInfo
   └─ 存入 openfpga_ctx.mutable_fabric_global_port_info()
   （无独立写命令；下游命令直接读 context）
```

#### 4.4.3 源码精读

**构建器入口与 circuit_library 来源**：

[openfpga/src/fabric/build_fabric_global_port_info.cpp:L41-L81](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_global_port_info.cpp#L41-L81) — 遍历 `find_circuit_library_global_ports(circuit_lib)`，在顶层模块按端口名 `find_module_port` 找到对应端口（找不到说明该全局端口未在顶层暴露，跳过），然后 `create_global_port` 并逐项设属性。这里体现了 u3-l3 讲过的「电路端口语义标志」（`port_is_reset`/`port_is_prog` 等）如何被 fabric 层消费。

**BL/WL 特判**（关键细节）：

[openfpga/src/fabric/build_fabric_global_port_info.cpp:L70-L80](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_global_port_info.cpp#L70-L80) — 对 `ql_memory_bank` + `shift_register` 协议，BL 和 WL 各有一根编程时钟。为区分「这根时钟属于 BL 还是 WL」，比较端口所属电路模型 `port_parent_model` 是否等于 `config_protocol.bl_memory_model()` / `wl_memory_model()`。这是 u9-l1（移位寄存器 bank）与本讲的又一个衔接点。

**tile_annotation 来源（含去重）**：

[openfpga/src/fabric/build_fabric_global_port_info.cpp:L83-L115](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_global_port_info.cpp#L83-L115) — 从 tile 级注解再收集一遍全局端口，但先用 `already_in_list` 跳过已由 circuit_library 加过的同名端口，避免重复。

**数据结构（SoA + 一堆布尔属性）**：

[openfpga/src/fabric/fabric_global_port_info.cpp:L96-L114](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_global_port_info.cpp#L96-L114) — `create_global_port` 把所有布尔标志初始化为 `false`、默认值初始化为 `0`，返回新 `FabricGlobalPortId`。每个访问器（[L33-L91](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_global_port_info.cpp#L33-L91)）都先 `VTR_ASSERT(valid_global_port_id(...))` 再取对应 `vtr::vector` 的下标，是典型的 SoA + 强类型 ID 模式（与 ModuleManager、CircuitLibrary 一致，见 u6-l1）。

**在 build_fabric 末尾存入 context**：

[openfpga/src/base/openfpga_build_fabric_template.h:L248-L252](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L248-L252) — `mutable_fabric_global_port_info() = build_fabric_global_port_info(...)`。注意它和 io_location_map（[L226](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L226)）一样，都是在 `build_fabric` 主流程跑完后、返回前构建并存入 context 的「收尾产物」。

**module_name_map 的消费点**：见 4.2.3 引用的 [fabric_hierarchy_writer.cpp:L80-L83](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_hierarchy_writer.cpp#L80-L83)，以及 hierarchy 模板里默认根模块取自 `module_name_map().name(generate_fpga_top_module_name())`（[openfpga_build_fabric_template.h:L319-L320](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_build_fabric_template.h#L319-L320)）。`rename_modules` 命令注册见 [openfpga_setup_command_template.h:L1567-L1575](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L1567-L1575)（依赖 `build_fabric`）。

#### 4.4.4 代码实践

> **实践目标**：理解 global port info 没有产物文件，但能在内存里被 testbench 命令读取。

**操作步骤（源码阅读型实践，无需运行）**：

1. 阅读 [build_fabric_global_port_info.cpp:L41-L81](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_global_port_info.cpp#L41-L81)，列出全局端口的属性清单（is_clock / is_set / is_reset / is_prog / is_shift_register / is_bl / is_wl / is_config_enable / is_io / default_value）。
2. 在仓库里搜索谁读取了 `fabric_global_port_info()`（用 Grep 找 `global_port_info` 的调用点），确认它们集中在 testbench / bitstream 生成。
3. 对比三类产物的「落盘方式」：fabric_key（XML 文件）、fabric_hierarchy（文本文件）、io_location_map（XML 文件）都有独立产物；而 global port info **只在内存**。

**需要观察的现象**：global port info 的消费者几乎都是 testbench 与 bitstream 相关命令，而非 PnR——印证了头文件注释「mainly used for testbench generation」（[fabric_global_port_info.h:L17-L22](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_global_port_info.h#L17-L22)）。

**预期结果**：能说清「为什么 global port info 没有写文件命令」——因为它是给 OpenFPGA 自己的 testbench/bitstream 生成器在内存里用的内部账本，不需要交给外部工具。

#### 4.4.5 小练习与答案

**练习 1**：global port info 的两个数据来源是什么？为什么要去重？
**答案**：来源 1 是 `circuit_library` 的全局端口（[L42-L81](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_global_port_info.cpp#L42-L81)），来源 2 是 `tile_annotation` 的全局端口（[L84-L115](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_global_port_info.cpp#L84-L115)）。去重是因为同一个端口可能同时被两处声明，避免重复登记导致 testbench 重复驱动。

**练习 2**：为什么 BL/WL 端口要专门特判？
**答案**：见 [L70-L80](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_global_port_info.cpp#L70-L80)。`ql_memory_bank` + `shift_register` 协议下，BL 和 WL 各有一根编程时钟，从电路模型看都是 clock 类型，必须靠「端口所属模型 == bl/wl_memory_model」来区分，否则 testbench 分不清哪根时钟驱动 BL、哪根驱动 WL。

**练习 3**：module_name_map 如何影响 hierarchy 输出？
**答案**：见 4.2.3。hierarchy writer 在输出每个模块名前先查 `module_name_map`，有别名就输出别名。配合 `rename_modules` 命令，用户可自定义对外模块名，hierarchy 树会跟着显示新名字，而 ModuleManager 内部标识不变。

---

## 5. 综合实践

**综合任务**：跑一次「同时产出 fabric key、hierarchy、io location map」的完整流程，并用源码解释每个产物里的一条具体记录是怎么来的。

建议步骤：

1. `source openfpga.sh`，参考 `generate_fabric_key_example_script.openfpga`（它在 `build_fabric --compress_routing --write_fabric_key ./fabric_key.xml` 之后还跟了 `write_fabric_hierarchy --file ./fabric_hierarchy.txt`）。把它改造或直接用对应任务跑通。

2. 在产物目录里收集三份文件：

   - `fabric_key.xml`
   - `fabric_hierarchy.txt`
   - （若脚本含 `write_fabric_io_info`）`fabric_io_location.xml`

3. 为每份文件挑一条记录，用本讲引用的源码行号解释：

   - fabric key：挑一个 `<key>`，解释它的 `name/value/column/row` 分别来自 [fabric_key_writer.cpp:L170-L182](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_key_writer.cpp#L170-L182) 的哪次赋值。
   - hierarchy：挑一行 `name:` 与一行 `- name`，解释为何一个走树风格、一个走列表风格（[fabric_hierarchy_writer.cpp:L119-L123](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_hierarchy_writer.cpp#L119-L123)）。
   - io location map：挑一条 pin 记录，解释其 index 来自哪个递增计数器（[build_fabric_io_location_map.cpp:L114-L122](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/build_fabric_io_location_map.cpp#L114-L122)）。

4. 进阶：把脚本里的 `build_fabric` 换成带 `--generate_random_fabric_key`，重新生成 fabric key 与比特流，对比两次 `fabric_key.xml` 里 key 的 `value`/顺序差异，验证「随机 key 打乱地址」的效果。

> 实际任务名以 `openfpga_flow/tasks` 真实目录为准；若环境无法运行，把第 3 步的「解释」做扎实即可，并标注「待本地验证」。

## 6. 本讲小结

- **fabric key 不是密钥**，而是「可配置存储器实例的地址编排清单」，记录每个配置位的 name/value/alias/坐标及所属 region；有两条产生路径——`build_fabric` 的 `--write_fabric_key/--load_fabric_key/--generate_random_fabric_key` 选项，与独立的 `write_fabric_key` 命令，底层都汇入 `write_fabric_key_to_xml_file()`。
- **`--generate_random_fabric_key`** 通过打乱存储器地址实现「混淆/安全」，配合复现用途的 `--load_fabric_key`，使电路相同但比特流地址布局可定制。
- **fabric hierarchy** 是带缩进的纯文本模块树，供层次化 PnR 使用；DFS 递归生成，支持 `--module/--filter/--depth/--exclude_empty_modules`，叶子与目标深度走 `- name` 列表风格、其余走 `name:` 树风格。
- **io_location_map** 在 `build_fabric` 内部构建并写入 context，建立「顶层 GPIO pin ↔ (x,y,z) 坐标」反查表，是 testbench 与 PCF 的共同依赖；可由 `write_fabric_io_info` 导出成 XML。
- **FabricGlobalPortInfo** 是全局端口属性账本（clock/set/reset/prog/BL/WL/...），从 `circuit_library` 与 `tile_annotation` 收集，仅在内存中供 testbench/bitstream 生成读取，**没有独立写文件命令**。
- 三个写命令（`write_fabric_key`/`write_fabric_hierarchy`/`write_fabric_io_info`）都注册在 **OpenFPGA setup** 命令组、都绑定 **const** 执行函数（只读 context）、都硬依赖 `build_fabric`。
- **module_name_map**（由 `rename_modules` 填充）让 hierarchy、verilog 等输出环节显示用户自定义模块名，是 `fpga_core` wrapper 自定义命名的基础。

## 7. 下一步学习建议

- **u7（比特流生成）**：fabric key 与 io_location_map 是比特流寻址与 IO 绑定的物理基础，u7-l3 讲 `build_fabric_bitstream` 时会直接消费这里的 region/坐标信息；`fast_configuration`（u7-l4）要读 `FabricGlobalPortInfo` 的 set/reset 端口。
- **u8-l2（testbench 生成）**：testbench 大量读取 `io_location_map`（IO 激励在哪）与 `FabricGlobalPortInfo`（哪个是 prog 时钟、复位默认值），可对照本讲的内存账本理解 testbench 行为。
- **u9-l1（移位寄存器 bank）**：本讲多次出现的 `blwl_sr_banks`、BL/WL 特判、`bl/wl_shift_register_banks` 节点，将在 u9-l1 系统讲解；建议读完后回看 [fabric_key_writer.cpp:L190-L229](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fabric/fabric_key_writer.cpp#L190-L229) 会有更深的理解。
- **u10-l3（PCF / name map 库）**：io_location_map 与 PCF 的协同、module_name_map 与 `libnamemanager` 的关系，在支撑库层面有更完整的实现。
