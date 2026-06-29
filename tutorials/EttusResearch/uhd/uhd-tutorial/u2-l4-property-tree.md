# 属性树 property_tree 机制

## 1. 本讲目标

上一讲（u2-l3）我们看到 `multi_usrp` 把「设采样率、设频率、设增益」包装成一行调用。但每次 `set_rx_rate`、`set_rx_freq` 背后到底改了什么？答案是：它们都在改一棵叫做 **属性树（property_tree）** 的内存数据结构。

本讲学完后，你应该能够：

1. 说清 `property_tree` 是什么、为什么 UHD 要用「文件系统」模型来组织设备配置。
2. 读懂 `property_tree.hpp` 的公共接口：`create` / `access` / `pop` / `list` / `exists` / `subtree`。
3. 理解树上的每个节点 `property<T>` 的「双值模型」（期望值 desired + 强制值 coerced）与回调机制（coercer / publisher / subscriber）。
4. 看懂 `property_tree.cpp` 里用嵌套 `dict` 实现一棵树、并用 `subtree` 共享底层存储的做法。
5. 通过 `multi_usrp::get_tree()` 直接读写底层节点，并解释 `/mboards/0/...` 这类路径的层级含义。

---

## 2. 前置知识

### 2.1 为什么需要一棵「树」？

一个 USRP 设备的可配置项是高度 **嵌套** 的：

```
设备
└─ 主板 mboards
   ├─ 0 号主板
   │   ├─ name / tick_rate / eeprom
   │   ├─ time（now / pps / cmd）
   │   ├─ rx_dsps（数字下变频核）
   │   └─ dboards（子板）
   │       └─ 某子板
   │           ├─ rx_frontends（接收前端：频率、增益、天线）
   │           └─ tx_frontends（发送前端）
   └─ 1 号主板 …
```

这种「一对多、层层深入」的结构，用扁平的 `std::map<string, value>` 很难自然表达。UHD 借用了 Unix 文件系统的思路：**用斜杠分隔的路径字符串定位配置项**，比如 `/mboards/0/dboards/A/rx_frontends/A/freq/value`。这就是 `property_tree`。

### 2.2 两个需要先记住的关键词

- **期望值（desired）**：用户请求的值，例如「我要 2.5 GHz」。
- **强制值（coerced）**：硬件实际能给的值，例如「硬件最近只能给 2.4999 GHz」。

属性树的每个节点都同时持有这两个值，本讲第 4.2 节会详细展开。

### 2.3 衔接上一讲

u2-l3 的核心结论之一是：`multi_usrp_impl` 几乎所有方法都是 **属性树的翻译层**——它把 `(chan, mboard)` 参数映射成树上的路径，再 `set/get`。本讲就来打开这层「翻译」看里面到底是什么。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [host/include/uhd/property_tree.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.hpp) | 公共接口：`property_tree` 抽象类、`property<T>` 节点接口、`fs_path` 路径字符串。 |
| [host/include/uhd/property_tree.ipp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.ipp) | 模板实现：`property_impl<T>`（节点的具体实现）与 `create/access/pop` 模板方法。 |
| [host/lib/property_tree.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp) | `property_tree_impl`：用嵌套 `dict` 实现整棵树，含 `fs_path` 工具函数与工厂 `make()`。 |
| [host/include/uhd/usrp/multi_usrp.hpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp) | 暴露 `get_tree()`，让用户能拿到底层属性树的句柄。 |
| [host/lib/usrp/multi_usrp.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp) | `mb_root` / `rx_dsp_root` / `rx_rf_fe_root` 等路径构造函数，是 `multi_usrp` 与树之间的桥梁。 |
| [host/utils/uhd_usrp_probe.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp) | `--tree` 选项递归打印整棵属性树，是最好的「活地图」。 |

---

## 4. 核心概念与源码讲解

### 4.1 property_tree：设备配置的「文件系统」模型

#### 4.1.1 概念说明

`property_tree` 是一个 **类型擦除（type-erased）的树状键值存储**：

- **树状**：用 `/` 分隔的路径（`fs_path`）定位，像文件系统目录。
- **键值存储**：每个「叶子」持有一个 `property<T>`，可读可写。
- **类型擦除**：树的内部存储不知道 `T` 是什么，存的是基类指针 `property_iface`；用户读写时再用模板 `access<T>` 把它「还原」回具体类型。

它的核心价值是：**让设备实现者把硬件状态以一棵树的形式注册出来，让上层（`multi_usrp`、`uhd_usrp_probe`、用户代码）用统一的路径语法去访问**，从而解耦「硬件长什么样」和「配置怎么读写」。

#### 4.1.2 核心流程

属性树对外暴露的最小操作集（见 [property_tree.hpp:221-267](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.hpp#L221-L267)）：

```text
make()              静态工厂，创建一棵空树
create<T>(path)     在路径上新建一个类型为 T 的节点（若已存在则抛异常）
access<T>(path)     取得路径上节点的引用，用于 .set() / .get()
pop<T>(path)        把节点从树上摘下来，返回其 shared_ptr
exists(path)        判断路径是否存在
list(path)          列出某「目录」下的所有子项名（类似 ls）
remove(path)        递归删除一个节点或子树
subtree(path)       返回一个以 path 为根的「视图」（共享底层存储）
```

读写一个值的完整链路是：

```text
用户调用 access<double>("/mboards/0/tick_rate")
   → 模板方法 _access() 在内部 dict 里按 "/" 切分路径，逐层下钻
   → 拿到节点里存的 property_iface 指针
   → dynamic_pointer_cast 回 property<double>
   → 返回引用，用户再 .set(25e6) 或 .get()
```

#### 4.1.3 源码精读

**公共抽象类与工厂**：[property_tree.hpp:221-240](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.hpp#L221-L240) 定义了抽象接口，其中 `make()` 是静态工厂，`coerce_mode_t` 枚举区分两种强制模式（本讲 4.2 详述）。

```cpp
class UHD_API property_tree : uhd::noncopyable
{
public:
    typedef std::shared_ptr<property_tree> sptr;
    enum coerce_mode_t { AUTO_COERCE, MANUAL_COERCE };
    virtual ~property_tree(void) = 0;
    static sptr make(void);                 // 工厂
    virtual sptr subtree(const fs_path& path) const = 0;
    virtual void remove(const fs_path& path) = 0;
    virtual bool exists(const fs_path& path) const = 0;
    virtual std::vector<std::string> list(const fs_path& path) const = 0;
    ...
};
```

工厂 `make()` 的实现极简——构造一个 `property_tree_impl`：[property_tree.cpp:237-240](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp#L237-L240)。

**路径字符串 `fs_path`**：[property_tree.hpp:206-216](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.hpp#L206-L216) 把 `std::string` 包装成「路径」，并提供 `leaf()`（取最后一段，类似 `basename`）、`branch_path()`（取父路径，类似 `dirname`），以及 `operator/` 拼接路径。

```cpp
struct UHD_API_HEADER fs_path : std::string {
    UHD_API fs_path(void);
    UHD_API fs_path(const char*);
    UHD_API fs_path(const std::string&);
    UHD_API std::string leaf(void) const;        // "a/b/c" → "c"
    UHD_API fs_path branch_path(void) const;     // "a/b/c" → "a/b"
};
UHD_API fs_path operator/(const fs_path&, const fs_path&);
UHD_API fs_path operator/(const fs_path&, size_t);  // 方便拼数字下标
```

`operator/` 的实现会自动剥掉左操作数的结尾 `/` 和右操作数的开头 `/`，所以 `/mboards/0` 与 `0` 拼接不会出现 `//`：[property_tree.cpp:48-67](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp#L48-L67)。

> 提示：初学者常被 `operator/`（除号）吓到。它在 UHD 里只是「路径拼接符」，和数学无关，纯粹为了写出 `mb_root(0) / "rx_dsps" / 0` 这样贴近文件系统直觉的代码。

#### 4.1.4 代码实践

**实践目标**：在不依赖硬件的前提下，用源码验证「属性树就是一棵可 `list` 的树」。

**操作步骤**（源码阅读型实践）：

1. 打开 [property_tree.hpp:240-255](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.hpp#L240-L255)，找到 `exists` / `list` / `create` / `access` / `pop` 五个方法。
2. 对照下表，把每个方法想象成一条 shell 命令：

| property_tree 方法 | 类似的 shell 命令 |
| --- | --- |
| `list(path)` | `ls path` |
| `exists(path)` | `test -e path` |
| `create<T>(path)` | `touch path`（并指定类型） |
| `access<T>(path)` | 打开文件准备读写 |
| `remove(path)` | `rm -r path` |

3. 若你已构建好 UHD 并有 USRP 硬件，可直接运行：

```bash
uhd_usrp_probe --tree
```

它会从根 `/` 开始递归 `list` 整棵树（见第 4.4.3 节）。

**需要观察的现象 / 预期结果**：无硬件时为「待本地验证」；有硬件时会看到形如 `/mboards/0/name`、`/mboards/0/rx_dsps/0/...` 的层级路径。**如果无法运行，标注「待本地验证」即可，不要假装已运行。**

#### 4.1.5 小练习与答案

**练习 1**：`fs_path("a/b/c").branch_path()` 返回什么？`leaf()` 呢？
**答案**：`branch_path()` 返回 `"a/b"`（去掉最后一段），`leaf()` 返回 `"c"`（最后一段）。依据 [property_tree.cpp:30-46](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp#L30-L46)。

**练习 2**：为什么 `property_tree` 要做成抽象基类 + `make()` 工厂，而不是直接 `new property_tree`？
**答案**：为了把接口（头文件里的抽象类）与实现（`.cpp` 里的 `property_tree_impl`）分离，用户只依赖稳定的抽象接口；同时为子树视图等特殊实现留出空间（见 4.3 节 `subtree`）。

---

### 4.2 property 节点：双值模型与回调机制

#### 4.2.1 概念说明

树上的每个叶子是一个 `property<T>`。它最核心的设计是 **双值模型**（见头文件注释 [property_tree.hpp:31-77](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.hpp#L31-L77)）：

- **desired（期望值）**：用户 `set()` 进来的值。
- **coerced（强制值）**：经过约束后真正生效的值。

很多 RF 参数（采样率、频率、增益）用户给的值不一定能被硬件精确满足。属性树不直接拒绝，而是 **接受 desired，由 coercer 算出 coerced**，再由读回路径返回 coerced。这就解释了 u1-l6 里「硬件会把请求采样率四舍五入到最接近支持值，必须回读」的现象。

围绕双值，`property<T>` 还提供三类回调：

| 回调 | 触发时机 | 数量限制 | 用途 |
| --- | --- | --- | --- |
| `coercer` | desired 改变后，算 coerced | 恰好一个 | 把 desired 约束到硬件可接受范围 |
| `subscriber`（desired / coerced 各一组） | 对应值可能改变时 | 零或多个 | 通知其它代码「值变了」，常用于联动寄存器 |
| `publisher` | `get()` 被调用时 | 至多一个 | 从硬件实时读回值，常用于只读属性 |

#### 4.2.2 核心流程

**`set(value)` 的执行顺序**（见 [property_tree.ipp:88-101](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.ipp#L88-L101) 与头文件注释 [property_tree.hpp:142-154](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.hpp#L142-L154)）：

```text
1. 写入 desired = value
2. 通知所有 desired subscriber
3. 若是 AUTO_COERCE：coerced = coercer(desired)
4. 通知所有 coerced subscriber
```

双值的关系可记为：

\[
\text{coerced} = \text{coerce}(\text{desired})
\]

若没有自定义 coercer，则使用默认恒等 coercer（输入即输出），此时 coerced ≡ desired。

**`get()` 的执行顺序**（见 [property_tree.ipp:111-125](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.ipp#L111-L125)）：

```text
若节点为空（从未 set 且无 publisher）→ 抛 runtime_error
若有 publisher → 返回 publisher()          // 实时读硬件
否则         → 返回缓存的 coerced 值
```

**两种强制模式**（`coerce_mode_t`，[property_tree.hpp:226](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.hpp#L226)）：

- `AUTO_COERCE`（默认）：必须有 coercer（没注册就用默认恒等 coercer），`set()` 会自动触发。
- `MANUAL_COERCE`：不走 coercer，由代码手动调 `set_coerced()` 写入强制值。

#### 4.2.3 源码精读

**默认恒等 coercer**：构造时若是 `AUTO_COERCE`，自动挂上 `DEFAULT_COERCER`（原样返回），保证「desired 必然能流向 coerced」：[property_tree.ipp:24-29](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.ipp#L24-L29) 与 [property_tree.ipp:142-145](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.ipp#L142-L145)。

**`set()` 的关键三步**（写入 desired → 通知 desired 订阅者 → 调 coercer 写 coerced）：

```cpp
property<T>& set(const T& value)
{
    init_or_set_value(_value, value);                 // ① 写 desired
    for (auto& dsub : _desired_subscribers)            // ② 通知 desired 订阅者
        dsub(get_value_ref(_value));
    if (_coercer) {                                    // ③ 算 coerced 并通知
        _set_coerced(_coercer(get_value_ref(_value)));
    } else {
        if (_coerce_mode == property_tree::AUTO_COERCE)
            uhd::assertion_error("coercer missing ...");
    }
    return *this;
}
```

见 [property_tree.ipp:88-101](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.ipp#L88-L101)，其中 `_set_coerced` 在 [property_tree.ipp:80-86](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.ipp#L80-L86)。

**`get()` 的 publisher 优先逻辑**：[property_tree.ipp:111-125](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.ipp#L111-L125)。若注册了 publisher，`get()` 永远走 publisher 实时取值，**不返回缓存**——这正是只读传感器（如温度、LO 锁定状态）的实现方式。

#### 4.2.4 代码实践

**实践目标**：理解 coercer 如何把 desired 变成 coerced。

**操作步骤**（源码阅读 + 心智推演）：

1. 读 [property_tree.hpp:88-98](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.hpp#L88-L98) 的 `set_coercer` 文档，确认「一个属性最多一个 coercer」。
2. 假设某属性的 coercer 是「把采样率对齐到 1 MHz 整数倍」，推演下表（**示例代码**，非项目原码）：

| 操作 | desired | coerced | `get()` 返回 |
| --- | --- | --- | --- |
| `set(2.5e6)` | 2.5e6 | 3.0e6 | 3.0e6 |
| `set(2.0e6)` | 2.0e6 | 2.0e6 | 2.0e6 |
| `get_desired()`（在 set(2.5e6) 之后） | — | — | 2.5e6 |

3. 对照 [property_tree.ipp:111-125](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.ipp#L111-L125) 与 [property_tree.ipp:127-134](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.ipp#L127-L134)，确认 `get()` 返回 coerced、`get_desired()` 返回 desired。

**需要观察的现象 / 预期结果**：`get()` 与 `get_desired()` 在有 coercer 时可能不同——这就是「回读实际值」的底层原因。运行验证为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：一个 `AUTO_COERCE` 的属性，如果开发者忘了调 `set_coercer`，`set()` 会出错吗？
**答案**：不会出错。构造时已自动挂上默认恒等 coercer `DEFAULT_COERCER`（[property_tree.ipp:24-29](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.ipp#L24-L29)），此时 coerced ≡ desired。只有显式调 `set_coercer` 第二次才会抛 assertion_error。

**练习 2**：为什么只读传感器属性通常配一个 publisher、而不设 subscriber？
**答案**：因为传感器值由硬件决定，不应被软件 `set()` 改写。挂 publisher 后，`get()` 每次都调用它实时读硬件（[property_tree.ipp:116-117](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.ipp#L116-L117)），且 publisher 属性永远 `empty()==false`（[property_tree.hpp:187-193](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.hpp#L187-L193)），天然适合只读。

---

### 4.3 property_tree_impl：用嵌套 dict 实现一棵树

#### 4.3.1 概念说明

`property_tree` 是接口，真正的实现在 [property_tree.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp) 里的 `property_tree_impl`。它的核心想法极其朴素：

> **一个目录节点 = 一个 `dict<string, 节点>`；节点要么是子目录（继续是 dict），要么挂着一个属性指针。**

这就是一个经典的 **前缀树（trie）**。外加两个工程细节：

1. **互斥锁**：整棵树共用一把 `std::mutex`，保证多线程 `get/set` 安全。
2. **subtree 共享底层存储**：`subtree()` 不复制树，而是新建一个带「根前缀」的 impl，**共享同一份 `_guts`**。

#### 4.3.2 核心流程

**节点定义**（[property_tree.cpp:212-222](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp#L212-L222)）：

```cpp
struct node_type : uhd::dict<std::string, node_type> {   // 既是目录又是节点
    std::shared_ptr<property_iface> prop;                // 叶子才非空
};
struct tree_guts_type {
    node_type root;        // 真正的树根
    std::mutex mutex;      // 全树共用锁
};
```

**所有操作的统一套路**（以 `_access` 为例，[property_tree.cpp:187-203](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp#L187-L203)）：

```text
1. path = _root / path_           // 拼上自己的根前缀
2. lock(_guts->mutex)             // 加锁
3. 用 path_tokenizer 按 "/" 切分 path
4. 从 _guts->root 出发，逐层 node = &(*node)[name] 下钻
5. 找不到就抛 lookup_error("Path not found")
6. 返回 node->prop
```

路径切分用的是一个宏（[property_tree.cpp:20-21](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp#L20-L21)），底层是 `boost::char_separator<char>("/")`。

**`subtree` 的共享语义**（[property_tree.cpp:80-88](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp#L80-L88)）：

```cpp
sptr subtree(const fs_path& path_) const override {
    const fs_path path = _root / path_;
    property_tree_impl* subtree = new property_tree_impl(path);
    subtree->_guts = this->_guts;   // ← 关键：共享同一份底层树！
    return sptr(subtree);
}
```

这意味着：对一个 subtree 的 `access` / `set`，会真实地改到原树，因为它们操作的是同一组 `node_type`。subtree 只是把「绝对路径」改写成「相对路径」的视图。

#### 4.3.3 源码精读

**`_create`：建路径时自动补齐中间目录**（[property_tree.cpp:167-185](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp#L167-L185)）：

```cpp
node_type* node = &_guts->root;
for (const std::string& name : path_tokenizer(path)) {
    if (not node->has_key(name)) {
        (*node)[name] = node_type();   // 中间目录不存在就自动建
    }
    node = &(*node)[name];
}
if (node->prop.get() != NULL)
    throw uhd::runtime_error("Cannot create! Property already exists at: " + path);
node->prop = prop;                     // 叶子挂上属性
```

注意两个保护：① 自动创建中间目录（类似 `mkdir -p`）；② 同一路径重复 `create` 会抛异常。

**`exists` 与 `list`**：[property_tree.cpp:110-123](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp#L110-L123) 与 [property_tree.cpp:125-139](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp#L125-L139)。两者都是下钻到目标节点后，`exists` 返回布尔，`list` 调 `node->keys()` 返回所有子项名。`list` 找不到路径会抛 `lookup_error`，`exists` 则静默返回 `false`。

**模板方法 `access<T>` 的类型还原**（[property_tree.ipp:186-195](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.ipp#L186-L195)）：

```cpp
template <typename T>
property<T>& property_tree::access(const fs_path& path)
{
    auto ptr = std::dynamic_pointer_cast<property<T>>(this->_access(path));
    if (!ptr) {
        throw uhd::type_error(
            "Property " + path + " exists, but was accessed with wrong type");
    }
    return *ptr;
}
```

这就是「类型擦除的还原点」：内部存的是 `property_iface` 基类指针，`access<T>` 用 `dynamic_pointer_cast` 还原回 `property<T>`。**如果用户用错了模板参数 `T`**（比如节点本是 `double`，却 `access<std::string>`），`dynamic_cast` 失败，抛出清晰的 `type_error`。

#### 4.3.4 代码实践

**实践目标**：验证 `subtree` 共享底层存储、以及类型用错的报错路径。

**操作步骤**（源码阅读型实践）：

1. 读 [property_tree.cpp:80-88](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp#L80-L88)，确认 `subtree` 只是把 `_guts` 这个 `shared_ptr` 复制了一份，没有深拷贝树。
2. 推演下面这段 **示例代码**（非项目原码）的输出：

```cpp
auto tree = property_tree::make();
tree->create<double>("/mboards/0/tick_rate").set(1e6);

auto sub = tree->subtree("/mboards/0");   // 取一个子树视图
sub->access<double>("tick_rate").set(2e6); // 通过子树改值

std::cout << tree->access<double>("/mboards/0/tick_rate").get(); // 打印什么？
```

3. 再推演：若把第二行换成 `tree->create<std::string>("/mboards/0/tick_rate")`（重复 create 同路径），会抛什么异常？若改成 `tree->access<int>("/mboards/0/tick_rate")`（类型用错）呢？

**需要观察的现象 / 预期结果**：

- 第 2 步打印 `2e6`——证明 subtree 改的就是原树（共享 `_guts`）。
- 重复 create 抛 `uhd::runtime_error`（[property_tree.cpp:180-183](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp#L180-L183)）。
- 类型用错抛 `uhd::type_error`（[property_tree.ipp:189-193](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.ipp#L189-L193)）。

实际运行为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `property_tree_impl` 的所有方法第一步都是 `_root / path_`？
**答案**：因为 `subtree()` 创建的 impl 带有不同的 `_root` 前缀却共享同一份 `_guts`。把 `_root / path_` 拼起来，才能让「子树视图里的相对路径」正确映射到全局存储里的绝对位置（见 [property_tree.cpp:80-88](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp#L80-L88)）。

**练习 2**：`exists` 与 `list` 对「路径不存在」的处理有何不同？为什么？
**答案**：`exists` 静默返回 `false`（[property_tree.cpp:116-122](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp#L116-L122)），因为它的语义就是「探测」；`list` 抛 `lookup_error`（[property_tree.cpp:131-135](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp#L131-L135)），因为对一个不存在的目录做 `ls` 本身就是错误。

---

### 4.4 multi_usrp 如何映射到属性树

#### 4.4.1 概念说明

`property_tree` 是设备实现者搭起来的「数据骨架」，`multi_usrp` 则是套在这个骨架上的「便利 API 外壳」。理解二者的映射关系，就理解了 u2-l3 里那句话：**「multi_usrp_impl 几乎所有方法都是属性树的翻译层」**。

这条翻译链由一组路径构造函数承担：

| 翻译函数 | 生成的树路径 | 含义 |
| --- | --- | --- |
| `mb_root(m)` | `/mboards/<m>` | 第 m 块主板 |
| `rx_dsp_root(chan)` | `/mboards/<m>/rx_dsps/<c>` | 接收 DSP 核（DDC） |
| `tx_dsp_root(chan)` | `/mboards/<m>/tx_dsps/<c>` | 发送 DSP 核（DUC） |
| `rx_rf_fe_root(chan)` | `/mboards/<m>/dboards/<db>/rx_frontends/<sd>` | 接收射频前端 |
| `tx_rf_fe_root(chan)` | `/mboards/<m>/dboards/<db>/tx_frontends/<sd>` | 发送射频前端 |

其中 `m`、`chan` 由 u2-l3 讲过的 `rx_chan_to_mcp`（通道→主板/通道对）和子设备规格 `subdev_spec` 决定。

#### 4.4.2 核心流程

一个 `multi_usrp::set_rx_rate(rate, chan)` 的完整下落过程：

```text
set_rx_rate(rate, chan)
  → rx_dsp_root(chan)                          // 算出路径 /mboards/0/rx_dsps/0
  → _tree->access<double>(path / "rate/value") // 取到 property<double>
  → .set(rate)                                  // 写 desired → coercer → coerced
  → 触发 coerced subscriber → 真正下发硬件寄存器
```

读回（`get_rx_rate`）则走同一条路径的 `.get()`，拿到的是被 coercer 修正后的 coerced 值——所以读回值可能与请求值不同。

用户也可以 **绕过 multi_usrp，直接操作树**：调 `multi_usrp::get_tree()` 拿到 `property_tree::sptr`，然后用 `access<T>(path)` 读写任意节点。这是进阶用法，`uhd_usrp_probe --tree` 与各种属性查询就是靠它实现的。

#### 4.4.3 源码精读

**`get_tree()` 入口**：[multi_usrp.hpp:139-141](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L139-L141)。注意其上方 `get_device()` 的注释（[multi_usrp.hpp:123-137](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L123-L137)）明确建议：**优先用 `get_tree()` 访问底层，而不是 `get_device()`**，且对 RFNoC 设备 `get_device()` 返回的是功能受限的对象。

**路径构造函数**（都在 [multi_usrp.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp)）：

```cpp
// 主板根：/mboards/<mboard>  (multi_usrp.cpp:2532-2546)
fs_path mb_root(const size_t mboard) {
    const std::string tree_path = "/mboards/" + std::to_string(mboard);
    if (_tree->exists(tree_path)) return tree_path;
    else throw uhd::index_error(...);
}

// 接收 DSP 根：/mboards/<m>/rx_dsps/<c>  (multi_usrp.cpp:2548-2575)
fs_path rx_dsp_root(const size_t chan) {
    mboard_chan_pair mcp = rx_chan_to_mcp(chan);
    ... // 可选的 rx_chan_dsp_mapping 重映射
    return mb_root(mcp.mboard) / "rx_dsps" / mcp.chan;
}

// 接收射频前端根：/mboards/<m>/dboards/<db>/rx_frontends/<sd>  (multi_usrp.cpp:2648-2660)
fs_path rx_rf_fe_root(const size_t chan) {
    mboard_chan_pair mcp = rx_chan_to_mcp(chan);
    const subdev_spec_pair_t spec = get_rx_subdev_spec(mcp.mboard).at(mcp.chan);
    return mb_root(mcp.mboard) / "dboards" / spec.db_name / "rx_frontends" / spec.sd_name;
}
```

注意 `mb_root` 会先用 `_tree->exists(...)` 校验路径存在（[multi_usrp.cpp:2536-2541](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L2536-L2541)），不存在就抛 `index_error`——这就是传错 `mboard` 时的报错来源。`tx_dsp_root`、`tx_rf_fe_root` 结构对称（[multi_usrp.cpp:2577-2603](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L2577-L2603)、[multi_usrp.cpp:2662-2674](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L2662-L2674)）。

**`multi_usrp` 实际读写树的代码**（真实调用，摘自 multi_usrp.cpp）：

```cpp
// 读主板名：access<std::string>(...).get()   (multi_usrp.cpp:302)
_tree->access<std::string>(mb_root(mcp.mboard) / "name").get();

// 读接收前端名：                          (multi_usrp.cpp:306)
_tree->access<std::string>(rx_rf_fe_root(chan) / "name").get();

// 写 tick_rate：access<double>(...).set()  (multi_usrp.cpp:389)
_tree->access<double>(mb_root(mboard) / "tick_rate").set(rate);

// 读回 tick_rate：                        (multi_usrp.cpp:399)
return _tree->access<double>(mb_root(mboard) / "tick_rate").get();

// 下发接收流命令：                        (multi_usrp.cpp:569)
_tree->access<stream_cmd_t>(rx_dsp_root(chan) / "stream_cmd").set(stream_cmd);
```

这五例覆盖了「读字符串 / 读写 double / 写复合类型」的典型模式——**先算路径，再 `access<T>(path)`，最后 `.get()` 或 `.set()`**。

**`uhd_usrp_probe --tree` 如何遍历整棵树**（[uhd_usrp_probe.cpp:328-335](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L328-L335)）：

```cpp
void print_tree(const uhd::fs_path& path, uhd::property_tree::sptr tree)
{
    for (const std::string& name : tree->list(path)) {
        print_tree(path / name, tree);   // 递归 list
    }
    // ... 打印 path 本身
}
```

它就是从 `/` 开始不断 `list` + 递归。命令行选项 `--tree` 定义在 [uhd_usrp_probe.cpp:413](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L413)，触发处在 [uhd_usrp_probe.cpp:506-507](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L506-L507)（`if (vm.count("tree") != 0) print_tree("/", tree);`）。而正常模式下 `get_device_pp_string` 会 `list("/mboards")` 逐块主板打印（[uhd_usrp_probe.cpp:317-323](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L317-L323)）。

#### 4.4.4 代码实践

**实践目标**：通过 `get_tree()` 列出一个 USRP 的关键路径并解释层级。

**操作步骤**：

1. **有硬件时（首选）**，编译并运行下面这段 **示例代码**（基于 [multi_usrp.hpp:139-141](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L139-L141) 的 `get_tree()`）：

```cpp
#include <uhd/usrp/multi_usrp.hpp>
#include <iostream>

int main() {
    uhd::device_addr_t addr("type=usrp1");        // 改成你的设备类型
    auto usrp = uhd::usrp::multi_usrp::make(addr);
    uhd::property_tree::sptr tree = usrp->get_tree();

    // 1) 列出所有主板
    for (auto& mb : tree->list("/mboards")) {
        std::cout << "mboard: " << mb << "\n";
        // 2) 列出该主板的顶层子项
        for (auto& k : tree->list("/mboards/" + mb)) {
            std::cout << "   " << k << "\n";
        }
    }
    // 3) 直接读一个已知节点
    std::cout << "name = "
              << tree->access<std::string>("/mboards/0/name").get() << "\n";
    return 0;
}
```

2. **没有硬件时**，运行 `uhd_usrp_probe --tree` 的等效分析：阅读 [uhd_usrp_probe.cpp:328-335](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L328-L335) 的 `print_tree`，在纸上画出它对 `/mboards/0` 递归 `list` 的展开顺序。

**需要观察的现象 / 预期结果**：

- 主板下应能看到 `name`、`eeprom`、`tick_rate`、`time`、`rx_dsps`、`tx_dsps`、`dboards`、`sensors` 等子项。
- `dboards/<db>/` 下又能看到 `rx_frontends` / `tx_frontends`，体现「主板 → 子板 → 前端」三级嵌套。
- 实际运行结果为「待本地验证」（依赖真实硬件）。

#### 4.4.5 小练习与答案

**练习 1**：调用 `usrp->set_rx_rate(2.5e6, /*chan=*/0)` 后，紧接着读 `/mboards/0/rx_dsps/0/rate/value`，得到的值一定是 `2.5e6` 吗？
**答案**：不一定。`set` 写的是 desired，节点上的 coercer 会把它修正成硬件支持的 coerced 值，`.get()` 读回的是 coerced（见 4.2 节）。这与 u1-l6「必须回读实际采样率」的结论一致。

**练习 2**：为什么 `rx_rf_fe_root` 的路径里同时有 `dboards/<db>` 和 `rx_frontends/<sd>` 两层？
**答案**：因为子板（dboard）可能包含多个射频前端（frontend）。`dboards/<db>` 定位物理子板，`rx_frontends/<sd>` 定位该子板上的某个接收前端（由子设备规格 `subdev_spec` 的 `sd_name` 决定，见 [multi_usrp.cpp:2648-2654](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L2648-L2654)）。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来——用 `get_tree()` 当作「显微镜」，观察一个 USRP 设备的属性树全貌，并解释清楚「一次 `set_rx_freq` 是如何沿着树流动的」。

**步骤**：

1. **画树**：运行 `uhd_usrp_probe --tree`（或在无硬件时阅读 [uhd_usrp_probe.cpp:328-335](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/utils/uhd_usrp_probe.cpp#L328-L335)），整理出 `/mboards/0` 下两层目录的树状图，标注哪些是「目录」（用 `list` 能列出子项），哪些是「叶子属性」（如 `name`、`tick_rate`）。

2. **定位路径**：对照 [multi_usrp.cpp:2648-2660](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/usrp/multi_usrp.cpp#L2648-L2660) 的 `rx_rf_fe_root`，写出 0 号通道接收频率对应的完整路径（应形如 `/mboards/0/dboards/A/rx_frontends/A/freq/value`）。

3. **追踪流动**：写出 `set_rx_freq(f, 0)` 从 API 调用到硬件寄存器的完整链路：

   ```text
   set_rx_freq(f, 0)
     → rx_rf_fe_root(0) / "freq/value"
     → _tree->access<double>(path).set(f)
        ① 写 desired=f
        ② 通知 desired subscriber
        ③ coercer 把 f 约束成 f'（合法频率）
        ④ 写 coerced=f'，通知 coerced subscriber
        ⑤ coerced subscriber 把 f' 写入硬件 LO 寄存器
     → get_rx_freq(0) 读回 f'（≠ f 时即发生了强制）
   ```

4. **验证双值**：若方便改代码，给目标频率属性临时挂一个 `add_coerced_subscriber`（参见 [property_tree.hpp:123-132](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.hpp#L123-L132)）打印强制值，观察 desired 与 coerced 的差异。**改源码仅为本地观察，不要提交**；无硬件则标注「待本地验证」。

**验收标准**：能画出至少两层的属性树、能写出频率属性的完整路径、能复述 `set` 的五步流动。这一关通过后，你就真正理解了 `multi_usrp` 那些「一行 API」背后的全部机制。

---

## 6. 本讲小结

- `property_tree` 是 UHD 设备配置的 **「文件系统」**：用 `/` 分隔的路径定位节点，对外提供 `create/access/pop/exists/list/remove/subtree` 七件事。
- 每个叶子节点是 `property<T>`，采用 **双值模型**：desired（用户请求）+ coerced（硬件实际可行），由 coercer 在两者间转换。
- 节点还支持三类回调：coercer（约束值，恰好一个）、subscriber（值变化通知，可多个）、publisher（实时读硬件，至多一个，常用于只读传感器）。
- 实现上 `property_tree_impl` 用 **嵌套 `uhd::dict`** 构成前缀树，全树共用一把互斥锁；`subtree()` 不复制树，而是 **共享底层 `_guts`**、仅改根前缀，所以子树视图的写会真实落到原树。
- 类型安全靠 `dynamic_pointer_cast`：内部存 `property_iface` 基类指针，`access<T>` 还原具体类型，用错 `T` 会抛清晰的 `type_error`。
- `multi_usrp` 是属性树的 **翻译层**：`mb_root/rx_dsp_root/rx_rf_fe_root` 等把 `(chan, mboard)` 映射成树路径，再 `access<T>().set/get()`；用户也可经 `get_tree()` 直接读写任意节点，`uhd_usrp_probe --tree` 正是这样遍历整棵树的。

---

## 7. 下一步学习建议

- **横向深入**：本讲只看了 `multi_usrp` 怎么用树。下一讲 **u2-l5（流式 API）** 会进入 `stream_args_t` 与 `rx_streamer/tx_streamer`，它们与属性树解耦，自己持有全部流式逻辑（见 [multi_usrp.hpp:123-128](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/usrp/multi_usrp.hpp#L123-L128) 的提示）。
- **纵向深入**：想看属性树是怎么被「搭起来」的，可读 u4-l4（MPMD 设备实现）——MPMD 在 `make` 阶段会逐条 `create<T>(path)` 构造出整棵树。
- **进阶机制**：属性树的 coercer/subscriber 是「单属性内」的联动；跨属性、跨块的自动依赖传播由 **experts 框架** 负责，对应 u3-l5，建议在学完 RFNoC 基础（u3-l1～u3-l3）后再读。
- **建议阅读源码**：[host/lib/property_tree.cpp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/lib/property_tree.cpp) 全文（仅 240 行）与 [host/include/uhd/property_tree.ipp](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/include/uhd/property_tree.ipp)，是理解本讲最直接的材料。
