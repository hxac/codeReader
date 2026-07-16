# 共享库 libvtrutil 与 VPR 工具

## 1. 本讲目标

本讲是「共享库、测试与工程实践」单元的第一讲，主题是 VTR 工程里**被所有 CAD 阶段复用**的两层基础设施：

- 底层共享库 `libvtrutil`：提供与 FPGA 无关的通用容器、强类型 ID、缓存、日志与错误处理。
- 上层 VPR 工具层 `vpr/src/util/`：在 `libvtrutil` 之上，封装跨阶段（打包/布局/布线/时序/图形）都要用的辅助函数。

学完本讲，你应当能够：

1. 说出 `vtr::vector`、`vtr::vector_map`、`vtr::NdMatrix`、`vtr::Cache` 各自的定位与适用场景。
2. 解释 `vtr::StrongId` 如何在**零运行时开销**下阻止 ID 类型混用，以及为什么它是上述容器的下标基础。
3. 区分 `VTR_LOG*` / `VTR_ASSERT*` / `VtrError` / `VPR_THROW` 四套机制的层次与职责。
4. 在 `vpr_utils` 中找到一个被多个阶段调用的辅助函数，并说明它如何读取全局上下文 `g_vpr_ctx`。

本讲依赖 [u3-l4 VprContext 与全局状态管理](u3-l4-vpr-context.md)：你需要知道 `g_vpr_ctx` 是全局状态总线，本讲里的跨阶段工具函数几乎都从它取数据。

## 2. 前置知识

阅读本讲前，最好已经理解以下概念（前三单元已建立）：

- **CAD 阶段与数据流**：`AtomNetlist → Packer → ClusteredNetlist → Placer → Router → Timing`，每个阶段读写不同的子上下文。
- **全局上下文 `g_vpr_ctx`**：所有阶段共享状态的「单一真相来源」，按 `device()` / `clustering()` / `placement()` / `routing()` 等子上下文切分。
- **C++ 模板与私有继承**：本讲的容器大量使用模板；`vtr::vector` 用 `private std::vector` 私有继承 + `using` 透传来「重定义下标类型」。
- **`size_t` 下标的局限**：普通 `std::vector` 用 `size_t` 下标，当你同时有 BlockId、NetId、PinId 时，编译器无法阻止你把一个 NetId 当 BlockId 用——这正是 `StrongId` 要解决的问题。

如果你对某块不熟，不必担心，下面会结合源码重新讲一遍。

## 3. 本讲源码地图

本讲涉及的关键文件分为两组：

| 文件 | 所属层 | 作用 |
| --- | --- | --- |
| `libs/libvtrutil/src/vtr_strong_id.h` | 共享库 | 定义强类型 ID 模板，是所有 ID 容器的下标基础 |
| `libs/libvtrutil/src/vtr_vector.h` | 共享库 | 用 StrongId 作下标的 `vector`，Netlist 等大量使用 |
| `libs/libvtrutil/src/vtr_vector_map.h` | 共享库 | 类 map 行为但底层是 vector，键须连续递增 |
| `libs/libvtrutil/src/vtr_ndmatrix.h` | 共享库 | N 维矩阵，单块线性内存 + 链式 `[]` 代理 |
| `libs/libvtrutil/src/vtr_cache.h` | 共享库 | 单槽缓存，按 key 命中即返回缓存值 |
| `libs/libvtrutil/src/vtr_log.h` | 共享库 | 日志宏 `VTR_LOG*` 与可插拔打印处理器 |
| `libs/libvtrutil/src/vtr_assert.h` | 共享库 | 四档断言宏 `VTR_ASSERT*` |
| `libs/libvtrutil/src/vtr_error.h` | 共享库 | 错误容器 `VtrError`（异常基类） |
| `libs/libvtrutil/src/vpr_error.h` | 共享库 | VPR 错误枚举 `e_vpr_error`、`VprError`、`VPR_THROW` 宏 |
| `vpr/src/util/vpr_utils.h` / `.cpp` | VPR 工具层 | 跨阶段辅助函数（物理类型查询、引脚范围、ID 转换等） |
| `vpr/src/util/vpr_net_pins_matrix.h` | VPR 工具层 | 用 `FlatRaggedMatrix` 表示「每网引脚数」的二维表 |

记住一条主线：**`libvtrutil` 是「与 FPGA 无关」的通用零件库；`vpr_utils` 是「懂 FPGA 语义」的跨阶段胶水**。后者站在前者的肩膀上。

---

## 4. 核心概念与源码讲解

### 4.1 通用容器与 ID 容器

#### 4.1.1 概念说明

VTR 的核心数据结构（`AtomNetlist`、`ClusteredNetlist`、`DeviceGrid`、各种上下文里的查找表）本质上都是「**用某种 ID 去查一条记录**」的表。如果直接用 `std::vector` + `size_t` 下标，会遇到两个工程痛点：

1. **ID 类型混用**：BlockId、NetId、PinId 底层都是整数，函数签名 `f(size_t)` 无法阻止你把 NetId 传进去，bug 极难排查。
2. **维度与稀疏性**：器件网格是三维 `[layer][x][y]`；每条网的引脚数各不相同（变长）。

`libvtrutil` 用一组模板容器对症下药：

- `StrongId`：给裸整数套一个编译期「幽灵 tag」，从根上杜绝类型混用。
- `vtr::vector<K, V>`：下标是 `K`（通常是某个 StrongId）的 vector，是 Netlist 的主力容器。
- `vtr::vector_map<K, V>`：想要 map 的 `find/insert` 语义、但键连续时的低开销替代。
- `NdMatrix<T, N>`：N 维稠密矩阵，单块线性内存。
- `Cache<K, V>`：单槽（只缓存最新一个 key）的惰性缓存。

#### 4.1.2 核心流程

**StrongId 如何做到「零开销类型安全」**：

1. 每个 ID 种类声明一个空 tag 结构体（如 `struct atom_block_id_tag;`）。
2. `StrongId<tag>` 把整数 `id_` 与 tag 绑定；只有 `explicit` 构造与显式转换，禁止隐式转换。
3. 不同 tag 的 StrongId 互相不能赋值/传参 → 编译期报错，运行期零成本。

**`vtr::vector` 如何重定义下标**：它**私有继承** `std::vector<V>`，再用 `using` 把绝大多数方法原样透传出来，唯独**不透传** `operator[]`/`at()`，而是重写成接收 `key_type`（即 StrongId），内部 `size_t(id)` 再委托回底层。

**连续性假设**：`vtr::vector` 与 `vtr::vector_map` 都假定 ID 是**从 0 开始、连续递增**的（这是 Netlist 的 `compress()` 之后成立的不变式，见 u3-l1）。这样「ID == 数组下标」，省掉哈希表，访问 O(1) 且缓存友好。

**NdMatrix 线性化**：N 维逻辑地址 `[i0][i1]...[iN-1]` 被映射到一维偏移：

\[
\text{offset} = \sum_{k=0}^{N-1} i_k \cdot \text{stride}_k
\]

其中最右维 stride 恒为 1（行主序），其余由 `resize()` 一次性算出（见源码精读）。链式 `[]` 返回临时 `NdMatrixProxy`，逐维剥皮，编译期被优化掉。

#### 4.1.3 源码精读

**① StrongId 的显式构造与显式转换**

`StrongId` 模板签名与「只允许显式构造」的约定见：

[libs/libvtrutil/src/vtr_strong_id.h:175-189](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_strong_id.h#L175-L189>) — 默认底层类型 `int`、哨兵值 `-1`；`StrongId(T id)` 标了 `explicit`，因此 `MyId id = 5;`（复制初始化）会被拒，必须 `MyId id(5);`。

它**只**开放三类显式转换，恰好覆盖「当布尔用」「当下标用」「打印」三种需求：

[libs/libvtrutil/src/vtr_strong_id.h:194-203](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_strong_id.h#L194-L203>) — `operator bool()`（`if(id)` 判合法）、`operator size_t()`（`vec[size_t(id)]` 下标）、`operator int()`（与 `INVALID()` 比较）。

> 注意：`size_t(id)` 必须**显式**写出来。这正是 `vtr::vector` 重写下标的伏笔——它替你做这一步。

**② vtr::vector：私有继承 + 重写下标**

[libs/libvtrutil/src/vtr_vector.h:51-52](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_vector.h#L51-L52>) — `class vector : private std::vector<V, Allocator>`，私有继承意味着「我借你的实现，但不暴露 is-a 关系」。

[libs/libvtrutil/src/vtr_vector.h:127-135](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_vector.h#L127-L135>) — 重写的 `operator[](const key_type id)`：先把 StrongId 显式转 `size_t`，再委托底层 `storage::operator[]`。于是 `block_locs[blk_id]` 既类型安全又 O(1)。

注释里明确写了它对键的连续性假设：

[libs/libvtrutil/src/vtr_vector.h:218-223](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_vector.h#L218-L223>) — 「all the underlying IDs are zero-based and contiguous」，所以 `key_iterator` 只需对 ID 加 1 即可遍历下一个键。

它还顺带提供了一个类似 Python `enumerate()` 的便利方法，可在范围 for 里同时拿到 `(id, value)`：

[libs/libvtrutil/src/vtr_vector.h:177-197](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_vector.h#L177-L197>) — `pairs()`，配合结构化绑定 `for (const auto& [blk_id, loc] : block_locs)`。

**③ vtr::vector_map：想要 map 语义时的低开销替代**

[libs/libvtrutil/src/vtr_vector_map.h:151-159](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_vector_map.h#L151-L159>) — `insert(key, value)`：若 key 超出当前容量就 `resize(key+1)`，**中间的空位用 `Sentinel::INVALID()` 填充**。所以它对「有空洞」的连续键也能工作，代价是浪费空间。

`vpr_utils` 里就能看到它的真实用法：布局的块位置表声明为 `vtr::vector_map<ClusterBlockId, t_block_loc>`：

[vpr/src/util/vpr_utils.h:46-47](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/util/vpr_utils.h#L46-L47>) — `get_sub_tile_index` 的参数类型正是这个容器。

**④ NdMatrix：N 维稠密矩阵**

实现注释说明了「单块线性数组、行主序、无逐维指针」的设计取舍：

[libs/libvtrutil/src/vtr_ndmatrix.h:166-176](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_ndmatrix.h#L166-L176>) — 目的是省内存、提升缓存命中。

`resize()` 在分配后一次性算出每维 stride（最右维恒为 1）：

[libs/libvtrutil/src/vtr_ndmatrix.h:269-276](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_ndmatrix.h#L269-L276>) — `dim_strides_[0] = size_ / dim_sizes_[0]`，其后 `dim_strides_[dim] = dim_strides_[dim-1] / dim_sizes_[dim]`。

链式 `[]` 由 `NdMatrixProxy` 逐维剥皮，最终命中一维基类返回真实元素：

[libs/libvtrutil/src/vtr_ndmatrix.h:387-398](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_ndmatrix.h#L387-L398>) — `operator[]` 返回低一维的 `NdMatrixProxy<T, N-1>`。

> **重要澄清（实践任务会用到）**：`NdMatrix` 的 `operator[]` 接收的是 **`size_t`，不是 StrongId**。它是为 `[layer][x][y]` 这类纯数值坐标网格设计的（u2-l3 的 `DeviceGrid` 正是用三维 `NdMatrix<t_grid_tile, 3>`）。真正用 StrongId 作下标的是 `vtr::vector` / `vtr::vector_map`，不要把两者混淆。

末尾还给了二维别名：

[libs/libvtrutil/src/vtr_ndmatrix.h:440-441](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_ndmatrix.h#L440-L441>) — `template<typename T> using Matrix = NdMatrix<T, 2>;`。

**⑤ vtr::Cache：单槽惰性缓存**

[libs/libvtrutil/src/vtr_cache.h:8-47](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_cache.h#L8-L47>) — 整个类只有一对 `key_` + `unique_ptr<CacheValue>`。它**只缓存最近一次**的键值，适合「同一 key 被高频反复查询、计算昂贵」的场景（如布线前瞻里按段查询预估代价）。

`get()` 命中条件是「key 相等且 value 非空」：

[libs/libvtrutil/src/vtr_cache.h:22-28](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_cache.h#L22-L28>) — 不命中返回 `nullptr`，由调用方决定是否重算并 `set()`。

#### 4.1.4 代码实践

**实践目标**：亲手验证「哪些容器用 StrongId 下标、哪些用 `size_t`」，并读懂一处真实 ID 容器声明。

**操作步骤**：

1. 打开 [vpr/src/base/vpr_context.h](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_context.h>) ，搜索 `vtr::vector<ClusterBlockId`。
2. 你会看到诸如 `vtr::vector<ClusterBlockId, bool> clb_in_flight;`、`vtr::vector<ClusterBlockId, std::mutex> mu;` 等声明——它们就是「以 ClusterBlockId（StrongId）为下标」的真实容器。
3. 对比打开 `libs/libvtrutil/src/vtr_ndmatrix.h`，查看 `operator[]` 的参数类型，确认它是 `size_t` 而非 StrongId。

**需要观察的现象**：

- `vtr::vector` 的下标类型是 `key_type`（StrongId），编译器会拒绝 `clb_in_flight[some_net_id]`。
- `NdMatrix` 的下标类型是 `size_t`，可以写 `grid[layer][x][y]` 但不能写 `grid[some_id]`。

**预期结果**：你应当能用一句话总结——**StrongId 下标用于「实体集合」（块/网/引脚），`size_t` 下标用于「数值坐标网格」（层/x/y）**。这一区分贯穿全工程。

> 如果你没有本地构建环境，本实践为「源码阅读型」，无需运行命令；若想进一步验证类型安全，可在本地写一个故意传错 ID 的小测试，预期得到编译错误。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `StrongId` 的构造函数要标 `explicit`？如果不标会怎样？

> **参考答案**：标 `explicit` 是为了禁止隐式转换。不标的话，`size_t x = 3; NetId n = x;` 会悄悄把一个普通整数当成 NetId，丧失类型安全；标了之后必须显式 `NetId n(x)`，让每一次「从数值造 ID」都成为可见的、可被审查的动作。

**练习 2**：`vtr::vector` 和 `vtr::vector_map` 都要求键连续，二者关键差别是什么？

> **参考答案**：`vtr::vector` 更接近 `std::vector`，只重定义下标类型、没有 `find/insert` 语义；`vtr::vector_map` 提供类 map 的 `find()/insert()/contains()`，且 `insert` 时会自动用哨兵值填充空洞。需要 map 式随机写入用 `vector_map`，纯数组式顺序构建用 `vector`（官方注释也建议优先 `vector`）。

**练习 3**：`vtr::Cache` 只缓存一个槽，为什么这种设计在某些场景下反而是优点？

> **参考答案**：当访问模式高度局部化（同一个 key 会被连续查询成百上千次，随后换成下一个 key）时，单槽缓存命中率高、内存与维护开销最小、无哈希表查找成本。它用极低复杂度换「热点命中」，适合布线这类对单点查询延迟敏感的内层循环。

---

### 4.2 日志与错误处理

#### 4.2.1 概念说明

一个跨越几十万行、十几个 CAD 阶段的工程，必须有统一的「说话方式」和「出错方式」。`libvtrutil` 提供三层：

1. **日志 `VTR_LOG*`**：正常运行时往屏幕/日志文件输出信息，分 info / warn / error 三种语气。
2. **断言 `VTR_ASSERT*`**：调试期的「内部不变式」检查，按开销分四档，release 可关。
3. **错误 `VtrError` / `VprError` / `VPR_THROW`**：运行期真正的失败（输入非法、阶段失败），抛异常终止并把错误归到某个阶段类别。

关键认知：**`VTR_LOG_ERROR` 只是打印，不会终止程序；想终止必须再 `throw` 一个 `VtrError`（通常用 `VPR_THROW` 宏）**。这一点头文件注释特别强调过，新手很容易踩坑。

#### 4.2.2 核心流程

**日志流向**：

1. 代码里写 `VTR_LOG_WARN("...")`。
2. 宏展开成条件 + 调打印处理器；warn 走 `print_or_suppress_warning()`（可被抑制重定向到噪音日志文件），error/info 走 `vtr::printf*`。
3. 处理器是**函数指针**（`PrintHandlerInfo` 等 `extern` 变量），可被全局替换——这就是「可插拔打印」。

**断言分档**：由 `VTR_ASSERT_LEVEL`（CMake 设定，默认 2）控制哪几档生效：

\[
\text{level} \ge k \Rightarrow \text{开销} \le \text{该档的断言被编译进二进制}
\]

四档从低到高：`OPT`(1) < `ASSERT`(2,默认) < `SAFE`(3) < `DEBUG`(4)。禁用时用 `sizeof(expr)` 技巧吃掉参数，避免「未使用变量」警告且零运行时开销。

**错误抛出链**：

1. `VPR_THROW(VPR_ERROR_ROUTE, "...")` 宏自动填入 `__FILE__`/`__LINE__`。
2. 调 `[[noreturn]] vpr_throw()` 构造 `VprError`（继承 `VtrError`，多了阶段类型 `type_`）。
3. 异常一路冒泡到 `main.cpp` 的三层 `catch`（见 u3-l5），按 `type()` 分类打印并退出。

#### 4.2.3 源码精读

**① 日志宏的三种语气 + 条件变体**

[libs/libvtrutil/src/vtr_log.h:59-68](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_log.h#L59-L68>) — `VTR_LOG` / `VTR_LOG_WARN` / `VTR_LOG_ERROR` 是「无条件」版；带 `V` 的 `VTR_LOGV*(expr, ...)` 是「条件」版，`expr` 为真才打印。

Debug 日志用编译期宏门控，默认关闭以省开销：

[libs/libvtrutil/src/vtr_log.h:121-127](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_log.h#L121-L127>) — 仅当定义了 `VTR_ENABLE_DEBUG_LOGGING` 时，`VTR_LOG_DEBUG` 才等同 `VTR_LOG`，否则被替换成无操作（NOP）。

打印处理是可替换的函数指针集合：

[libs/libvtrutil/src/vtr_log.h:136-140](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_log.h#L136-L140>) — `extern PrintHandlerInfo printf;` 等，外部可重定向输出（例如 GUI 把日志写到窗口）。

**② 断言四档与 `sizeof` 技巧**

[libs/libvtrutil/src/vtr_assert.h:89-94](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_assert.h#L89-L94>) — 生效版：条件失败就调 `vtr::assert::handle_assert(...)`（标了 `[[noreturn]]`）。

[libs/libvtrutil/src/vtr_assert.h:109-113](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_assert.h#L109-L113>) — 禁用版：用 `sizeof(expr)` 吃掉表达式（标准保证 `sizeof` 的操作数不求值），既消除未使用警告又零开销。

**③ 错误容器 `VtrError`**

[libs/libvtrutil/src/vtr_error.h:34-40](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vtr_error.h#L34-L40>) — 继承 `std::runtime_error`，额外携带 `filename_` 与 `linenumber_`，让上层 catch 能打印「文件名:行号」。

**④ VPR 的阶段化错误 `VprError` 与 `VPR_THROW`**

错误按 CAD 阶段分类，便于上层精准报告：

[libs/libvtrutil/src/vpr_error.h:8-35](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vpr_error.h#L8-L35>) — `e_vpr_error` 枚举，含 `VPR_ERROR_ARCH / PACK / PLACE / AP / ROUTE / TIMING / POWER / SDC` 等。

`VprError` 在 `VtrError` 基础上加一个阶段类型字段：

[libs/libvtrutil/src/vpr_error.h:41-54](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vpr_error.h#L41-L54>) — 构造时传入 `t_vpr_error_type`，`type()` 取回。

抛错宏自动填文件行号，且被调函数标 `[[noreturn]]`：

[libs/libvtrutil/src/vpr_error.h:96-99](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vpr_error.h#L96-L99>) — `VPR_THROW(type, ...)` → `vpr_throw(type, __FILE__, __LINE__, ...)`。

另外 `VPR_ERROR(...)`（非致命、可被降级为警告）走 `vpr_throw_opt()`：

[libs/libvtrutil/src/vpr_error.h:125-128](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/libs/libvtrutil/src/vpr_error.h#L125-L128>) — 多带函数名信息，便于把某些函数里的错误降级为 `VTR_LOG_WARN`。

#### 4.2.4 代码实践

**实践目标**：在真实源码里找到「先 `VTR_LOG_ERROR` 打印、再 `VPR_THROW` 终止」的标准范式。

**操作步骤**：

1. 用搜索工具在 `vpr/src/` 下查找同时出现 `VTR_LOG_ERROR` 与 `VPR_THROW` 的函数（例如解析器、校验函数）。
2. 阅读该函数，确认打印的报错信息与抛出的错误类型是否一致（例如布局问题抛 `VPR_ERROR_PLACE`）。

**需要观察的现象**：

- 单独的 `VTR_LOG_ERROR` 之后若没有 `throw`，程序会继续运行——这正是头文件注释提醒的坑。
- `VPR_THROW` 的第一个实参总是某个 `VPR_ERROR_*` 枚举，与所在阶段对应。

**预期结果**：你能复述本节的核心结论——**「打印错误」与「抛出错误」是两件事**；终止程序必须抛 `VprError`，阶段分类靠 `e_vpr_error` 枚举。待本地验证（具体函数名以你本地仓库检索结果为准）。

#### 4.2.5 小练习与答案

**练习 1**：`VTR_LOG_ERROR("x")` 之后程序会停吗？怎么让它停？

> **参考答案**：不会停。`VTR_LOG_ERROR` 只打印。要停必须在打印之后 `throw` 一个 `VtrError`，工程里通常用 `VPR_THROW(VPR_ERROR_*, "...")`，它会构造带阶段类型与文件行号的异常并终止当前流程。

**练习 2**：`VTR_ASSERT_SAFE` 和 `VTR_ASSERT` 在 release 构建里有什么区别？

> **参考答案**：默认 `VTR_ASSERT_LEVEL=2` 时，`VTR_ASSERT`（档 2）生效、`VTR_ASSERT_SAFE`（档 3）被编译成 NOP。`SAFE` 用于较昂贵的不变式检查，只在调试构建（level≥3）启用；release 里它零开销，从而不拖慢线上性能。

**练习 3**：为什么 `VprError` 要单独维护一个 `t_vpr_error_type` 字段，而不直接复用 `std::runtime_error`？

> **参考答案**：`std::runtime_error` 只有 `what()` 字符串，无法被 catch 代码「按阶段」精准处理。`t_vpr_error_type` 让上层（`main.cpp` 的 catch 链）能据 `type()` 区分「这是架构错误还是布线错误」，给出针对性的提示与退出码，也便于测试按类型断言。

---

### 4.3 VPR 跨阶段工具

#### 4.3.1 概念说明

`libvtrutil` 故意「不懂 FPGA」——它连 `t_pb_type` 都不认识。真正**懂 FPGA 语义**的共享逻辑放在 `vpr/src/util/`。其中最核心的是 `vpr_utils.h` / `.cpp`：一组**被多个 CAD 阶段反复调用**的小函数，专门解决「给定一个块/引脚/RR 节点，查它的物理类型、引脚范围、子瓦片、class 范围」这类横切问题。

为什么需要这层？因为这些查询同时依赖「器件网格（device）」「聚簇网表（clustering）」「布局位置（placement）」三个子上下文，**任何单个阶段目录都不「拥有」它们**，放进 `vpr_utils` 才能避免各阶段各自重写一份、产生不一致。

典型成员：

- `physical_tile_type(...)`：三重载，从坐标 / 原子块 / 父块 ID 拿到物理瓦片类型。
- `get_sub_tile_index(...)`：块落在某瓦片的哪个子瓦片。
- `get_class_range_for_block(...)` / `get_pin_range_for_block(...)`：块的引脚 class 与物理引脚范围。
- `is_inter_cluster_node(...)`：某 RR 节点是不是「簇间」节点（vs 簇内节点）。
- `convert_to_*_id(...)`：在 AtomNetlist 与 ClusteredNetlist 的 ID 之间安全转换。

另外 `vpr_net_pins_matrix.h` 提供了「每网引脚数各不同」的变长二维表 `NetPinsMatrix`，是 `net_delay` 等数据的载体（u7-l2 已用到）。

#### 4.3.2 核心流程

**跨阶段工具函数的典型数据流**（以 `physical_tile_type` 为例）：

```
调用方(任意阶段)
   │  传入 ClusterBlockId / AtomBlockId / t_pl_loc
   ▼
physical_tile_type(...)            ← vpr_utils.cpp
   │  取 g_vpr_ctx.placement().block_locs()[blk].loc   // 查布局坐标
   │  取 g_vpr_ctx.device().grid.get_physical_type(loc) // 查器件网格
   ▼
返回 t_physical_tile_type_ptr
```

要点：

1. 工具函数**只读**全局上下文，不修改状态——它是纯查询。
2. 通过**重载**屏蔽「输入是原子块、聚簇块还是坐标」的差异，统一返回物理瓦片类型。
3. 因为读取的是 `g_vpr_ctx`，所以「布局还没跑」时调用布局相关的重载会拿到无效坐标——调用时机的责任在调用方。

**ID 转换的语义**：`ParentBlockId` 是「扁平/非扁平」统一标识（见 u8-l5 的扁平布线）。`convert_to_cluster_block_id` / `convert_to_atom_block_id` 在 `is_flat` 分支里把 `ParentBlockId` 落到对应的具体 ID 类型。

#### 4.3.3 源码精读

**① `physical_tile_type` 的三个重载（声明）**

[vpr/src/util/vpr_utils.h:39-43](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/util/vpr_utils.h#L39-L43>) — 分别接收 `t_pl_loc`（坐标）、`AtomBlockId`（原子块）、`(ParentBlockId, bool is_flat)`（统一标识）。同一名字、三种入口，是典型的「重载式多态」。

**② 实现里如何串起 placement 与 device 上下文**

[vpr/src/util/vpr_utils.cpp:377-381](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/util/vpr_utils.cpp#L377-L381>) — 坐标版最底层：直接 `device_ctx.grid.get_physical_type({x, y, layer})`。

[vpr/src/util/vpr_utils.cpp:383-391](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/util/vpr_utils.cpp#L383-L391>) — 原子块版：先用 `atom_look_up.atom_clb(atom_blk)` 把原子块映射到聚簇块，再查它的布局坐标，最后**委托回坐标版**。注意这里的 `VTR_ASSERT(cluster_blk != ClusterBlockId::INVALID())`——典型的内部不变式断言。

[vpr/src/util/vpr_utils.cpp:393-403](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/util/vpr_utils.cpp#L393-L403>) — 父块版：按 `is_flat` 分叉，扁平走原子块路径、非扁平走聚簇块路径。这就是扁平布线（u8-l5）与经典流程共用同一套工具函数的关键。

> 观察设计：三个重载彼此**层层委托**，最终都落到「坐标 → 网格查询」这一最底层动作。没有任何重载直接复制底层逻辑，避免了不一致。

**③ `is_inter_cluster_node`：布线/图形/校验都要用的判断**

声明：

[vpr/src/util/vpr_utils.h:251-252](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/util/vpr_utils.h#L251-L252>) — 接收 RR 图只读视图 `RRGraphView` 与一个 `RRNodeId`。

实现：

[vpr/src/util/vpr_utils.cpp:1644-1662](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/util/vpr_utils.cpp#L1644-L1662>) — 通道类节点（`CHANX/CHANY/CHANZ/MUX`）直接判为簇间；对 `IPIN/OPIN` 看 `is_pin_on_tile`、对 `SINK/SOURCE` 看 `is_class_on_tile`，即「该引脚/class 是否位于瓦片顶层（而非簇内 pb 上）」。这一判断把 RR 图节点类型（u6-l1）与瓦片物理结构（u2-l2）对接起来，是布线、图形高亮、路由校验共用的横切逻辑。

**④ ID 转换模板**

[vpr/src/util/vpr_utils.h:91-103](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/util/vpr_utils.h#L91-L103>) — `convert_to_cluster_block_id<T>(T id)` 等：把任意可 `size_t()` 的 ID 显式构造为目标 StrongId 类型。模板化避免为每对 ID 组合手写函数。

**⑤ `NetPinsMatrix`：变长「网—引脚」表**

[vpr/src/util/vpr_net_pins_matrix.h:9-19](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/util/vpr_net_pins_matrix.h#L9-L19>) — `NetPinsMatrix_<T, NetId> = vtr::FlatRaggedMatrix<T, NetId, int>`，并给出 `NetPinsMatrix` / `ClbNetPinsMatrix` / `AtomNetPinsMatrix` 三个别名，分别对应父网表、聚簇网表、原子网表。

工厂函数按「每网实际引脚数」构造不规则矩阵：

[vpr/src/util/vpr_net_pins_matrix.h:21-28](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/util/vpr_net_pins_matrix.h#L21-L28>) — `make_net_pins_matrix(nlist, default)` 用 `nlist.net_pins(net).size()` 作为每网的行长度。这正是 u7-l2 里 `net_delay`（`NetPinsMatrix<float>`）的载体，避免用「最大引脚数 × 网数」的稠密矩阵浪费内存。

#### 4.3.4 代码实践

**实践目标**：写出一个被多处调用的 `vpr_utils` 辅助函数的「调用画像」，并解释它为何必须跨阶段共享。

**操作步骤**：

1. 选定目标函数 `is_inter_cluster_node`（或 `physical_tile_type`）。
2. 用搜索工具统计它在 `vpr/src/` 下的出现位置（命令示例：`grep -rn "is_inter_cluster_node" vpr/src/`）。
3. 把命中文件按 CAD 阶段归类（route / draw / pack / base）。

**需要观察的现象**（以 `is_inter_cluster_node` 为例）：

- 它出现在 `route/`（布线校验、过载报告）、`draw/`（图形绘制 RR 节点）、`base/`（读回路由、统计）等多个目录。
- 各调用点的签名一致，都先取 `RRGraphView` 再调本函数，无人各自重写判断逻辑。

**预期结果**：你应当能写出类似下面的「调用画像」（具体文件以本地检索为准，待本地验证）：

```
is_inter_cluster_node(rr_graph_view, node_id)
  ├─ route/check_route.cpp        // 布线合法性校验：区分簇内/簇间
  ├─ route/overuse_report.cpp     // 过载报告：只报簇间资源
  ├─ draw/draw_rr.cpp             // 图形：按节点是否簇间着色
  └─ base/stats.cpp               // 统计：分别计数两类节点
```

并解释：因为「节点是否属于簇间」这一语义同时依赖 RR 图（device）与瓦片结构，不属于任何单一阶段，所以必须放在共享的 `vpr_utils` 里，否则每个阶段重写一份会产生不一致。

#### 4.3.5 小练习与答案

**练习 1**：`physical_tile_type` 为什么用三个重载而不是一个带可选参数的函数？

> **参考答案**：三个重载对应三种语义不同的输入（坐标、原子块、统一父块），参数类型不同、还牵涉 `is_flat` 布尔。用重载让调用点写 `physical_tile_type(x)` 即可，编译器按实参类型分派；若合并成一个带可选参数的函数，反而要把「坐标 vs ID」的歧义塞进运行期判断，丢失类型安全。三个重载又彼此委托到坐标版，逻辑只在最底层写一遍。

**练习 2**：如果布局阶段还没运行，调用 `physical_tile_type(AtomBlockId)` 会发生什么？

> **参考答案**：它会去取 `placement().block_locs()[cluster_blk].loc`，而此时 `block_locs` 尚未填好有效坐标，得到的 `loc` 无意义（甚至可能命中断言失败）。这说明这类工具函数**隐含调用时机契约**：依赖布局结果的重载必须在布局完成后调用。责任在调用方，函数本身不做「是否已布局」的防御。

**练习 3**：`NetPinsMatrix` 为什么用「不规则（ragged）矩阵」而不是 `NdMatrix`？

> **参考答案**：每条网的引脚数差异很大（一个时钟网可能有上千引脚，一个普通组合网只有两三个）。用稠密 `NdMatrix` 必须按「最大引脚数」分配每行，绝大多数空间浪费。`FlatRaggedMatrix` 按每网实际引脚数分行，紧凑且仍能 O(1) 按 `[net][pin]` 访问，是用「行长度不规则」换内存效率的典型设计。

---

## 5. 综合实践

把本讲三块知识串起来，完成一个小型「源码考古」任务：

**任务**：追踪一个具体类型 `vtr::vector<ClusterBlockId, t_block_loc>` 在工程里的完整生命周期。

**步骤**：

1. **定位容器**：在 `vpr/src/base/` 下找到布局位置表 `block_locs()` 的声明（提示：它在 `PlacementContext` 相关代码里，类型是 `vtr::vector_map<ClusterBlockId, t_block_loc>` 或 `BlkLocRegistry` 内的 `vtr::vector`）。确认它的下标是 `ClusterBlockId`（StrongId），这正是 4.1 讲的「StrongId 下标容器」。
2. **定位写入方**：搜索谁对这个表做了 `mutable` 写入（布局阶段，u5）。体会 u3-l4 讲的「生产者取 mutable、消费者取 const」。
3. **定位读取方**：找到至少两个**不同阶段**的读取者调用 `physical_tile_type(blk)` 或 `get_sub_tile_index(blk)`（4.3 讲的跨阶段工具），它们经由这张表把 `ClusterBlockId` 翻译成物理瓦片。
4. **观察错误处理**：在 `get_sub_tile_index` 的实现里（[vpr/src/util/vpr_utils.cpp:405-429](<https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/util/vpr_utils.cpp#L405-L429>)），看它如何在块落在不兼容子瓦片时用 `VPR_THROW(VPR_ERROR_PLACE, ...)`（4.2 讲的阶段化错误）终止。

**产出**：画一张时序图，标出「StrongId 容器 → 全局上下文 → 跨阶段工具函数 → 阶段化错误」这条链路上的至少 4 个节点，并注明每个节点对应的本讲知识点编号。

> 这个任务无需编译运行，是「源码阅读型综合实践」。如果你本地已构建（见 u1-l2），可额外在 `get_sub_tile_index` 里临时加一行 `VTR_LOG(...)` 观察运行期调用频率——但**不要提交对该文件的修改**，验证后请还原。待本地验证。

## 6. 本讲小结

- `libvtrutil` 是与 FPGA 无关的通用零件库；`vpr/src/util/` 是懂 FPGA 语义的跨阶段胶水，后者站在前者肩膀上。
- `StrongId` 用编译期幽灵 tag + `explicit` 构造，在零运行时开销下阻止 ID 类型混用；它是 `vtr::vector` / `vtr::vector_map` 的下标基础。
- `vtr::vector` 私有继承 `std::vector` 并重写下标为 StrongId；`vtr::vector_map` 额外提供 map 式 `find/insert` 并自动用哨兵填空洞；二者都要求键从 0 连续。
- `NdMatrix` 用单块线性内存 + 链式 `NdMatrixProxy` 表达 N 维稠密矩阵，下标是 `size_t`（**不是** StrongId），用于坐标网格（如 `DeviceGrid`）。
- 日志/断言/错误是三层：`VTR_LOG*` 只打印（`VTR_LOG_ERROR` 不停机）；`VTR_ASSERT*` 分四档、release 可关；想停机须 `VPR_THROW` 抛 `VprError`，其阶段类型由 `e_vpr_error` 枚举刻画。
- `vpr_utils` 的跨阶段函数（如 `physical_tile_type` 三重载、`is_inter_cluster_node`）只读 `g_vpr_ctx`、彼此层层委托、避免各阶段重写不一致——这是「横切逻辑集中化」的典范。

## 7. 下一步学习建议

- **横向深入容器**：阅读 `vtr_ragged_matrix.h`（`NetPinsMatrix` 的底层）、`vtr_linear_map.h`、`vtr_bimap.h`，对比它们与 `vector` / `vector_map` 的取舍，建立完整的「libvtrutil 容器选型表」。
- **纵向进入测试**：本单元下一讲 [u9-l2 单元测试体系 Catch2](u9-l2-unit-tests.md) 将讲解如何为这些容器与工具函数写单元测试；建议先看 `libs/libvtrutil/test/` 下对 `vtr_vector`、`vtr_ndmatrix`、`vtr_strong_id` 的测试，它们是最好的使用范例。
- **回归与 QoR**：随后阅读 [u9-l3 回归测试与 QoR 评估](u9-l3-regression-and-qor.md)，理解这些底层工具的改动如何被回归测试体系保护。
- **回看依赖**：若对工具函数读取的上下文还不够熟，回顾 [u3-l4 VprContext 与全局状态管理](u3-l4-vpr-context.md) 与 [u3-l5 主流程编排 vpr_api](u3-l5-vpr-api-orchestration.md)，能更好理解「跨阶段工具函数为何只读 `g_vpr_ctx`」。
