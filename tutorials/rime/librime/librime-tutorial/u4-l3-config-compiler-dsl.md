# 配置编译器：__include / __patch DSL

## 1. 本讲目标

本讲拆解 librime 配置系统的「编译器」——`ConfigCompiler`。在上一篇 u4-l2 中，我们把 `ConfigCompiler` 当作黑盒：只知道当 `ConvertFromYaml` 的 `compiler` 参数非空时，YAML 就具备了 `__include` / `__patch` 这样的「配置编程」能力。

读完本讲，你应当能够：

1. 说清 `__include` / `__patch` / `__append` / `__merge` 四类指令各自做什么、何时触发。
2. 看懂 `/+`、`/=`、`@next`、`N+` 等键名后缀运算符的语义。
3. 理解配置节点之间的「依赖」是如何被建模成一张图、按优先级排序、并惰性解析的。
4. 解释循环依赖为何不会让编译器死循环，以及 `resolve_chain` 是如何检测它的。
5. 描述「写时拷贝（Copy-On-Write，COW）」是如何保证对一个方案的 `__patch` 不会污染被 `__include` 进来的原始数据的。

---

## 2. 前置知识

本讲假设你已经掌握 u4-l1（`ConfigItem` / `ConfigValue` / `ConfigList` / `ConfigMap` 的类型层次）与 u4-l2（`Config::Component` 工厂、`ConfigData` 树、`ResourceResolver` 资源解析、`ConfigLoader` 与 `ConfigBuilder` 的分水岭）。这里再强调三个本讲会反复用到的概念：

- **ConfigItemRef（节点引用代理）**：u4-l1 提到的 `ConfigItemRef` 是「指向树中某个位置的代理」，对它赋值 `*ref = item` 等价于把 `item` 写回树中那个位置。本讲的 include/patch 几乎所有写操作都通过 `ConfigItemRef` 完成。
- **resource_id 与 local_path**：一个配置资源（一个 `.yaml` 文件）在编译器里用 `resource_id`（如 `starcraft`，对应 `starcraft.yaml`）标识；文件内部某个节点用斜杠路径 `local_path`（如 `/terrans/player`）定位。二者拼成「合格路径」`starcraft:/terrans/player`。
- **构建期 vs 运行期**：`__include` / `__patch` 只在「构建期」（部署、`ConfigBuilder` 加载方案时）被求值；一旦编译完成，产物就是一棵普通的 `ConfigItem` 树，运行期 `Config` 读到的已是展开后的结果。这也是为什么 u4-l2 把它叫「带编译插件」的加载路径。

> 名词提醒：本讲的「编译」与 C++ 编译无关，它指 **YAML 配置 DSL 的展开与链接**，发生在 `ConfigBuilder` 读取 `.yaml` 的过程中。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/rime/config/config_compiler.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.h) | `ConfigCompiler` 类对外接口、四类指令常量、`Reference` / `ConfigResource` 结构。 |
| [src/rime/config/config_compiler.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc) | 编译器主体：指令解析、依赖图构建、依赖解析、节点编辑（覆盖/追加/合并）、循环检测。 |
| [src/rime/config/config_compiler_impl.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler_impl.h) | 依赖类型族：`Dependency` 基类与 `PendingChild` / `IncludeReference` / `PatchReference` / `PatchLiteral` 四个子类，以及优先级枚举。 |
| [src/rime/config/config_cow_ref.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_cow_ref.h) | `ConfigCowRef<T>`：写时拷贝节点引用模板，是 include/patch 不污染原始数据的底层保证。 |
| [src/rime/config/config_data.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_data.cc) | `ConvertFromYaml`（调用 `compiler->Parse` 的入口）、`TypeCheckedCopyOnWrite` / `TraverseCopyOnWrite`、路径工具 `SplitPath` / `JoinPath` / `ResolveListIndex`。 |
| data/test/config_dependency_test.yaml, config_merge_test.yaml, config_compiler_test.yaml, config_circular_dependency_test.yaml | 配套测试数据，本讲大量示例直接取自这里。 |

---

## 4. 核心概念与源码讲解

本讲把 DSL 拆成五个最小模块：①四类指令与编译入口；②依赖图与优先级；③依赖解析与循环检测；④include / patch 的执行语义；⑤节点编辑三态、后缀运算符与写时拷贝。

### 4.1 四类指令与编译入口

#### 4.1.1 概念说明

`ConfigCompiler` 定义了四个保留键（即「指令」），它们在 YAML 里以普通键的形式出现，但语义特殊：

| 指令 | 含义 |
| --- | --- |
| `__include` | 把另一处节点（本文件或外部文件）的内容「整体搬进来」作为当前节点的值。 |
| `__patch` | 对当前节点做「局部修改」：可以是引用另一处 patch 映射、直接写一张 `{路径: 值}` 字面量表，或一个这样的列表。 |
| `__append` | 把一个列表追加到当前（列表）节点的末尾。是 `/+` 后缀的等价指令形式。 |
| `__merge` | 把一个映射深合并进当前（映射）节点。 |

注意 `__append` / `__merge` 并不直接在 `ConfigCompiler::Parse` 里分发——它们是**节点编辑阶段**（`EditNode`）通过键名识别的（见 4.5）。`Parse` 只负责识别 `__include` 和 `__patch`，把它们转换成「依赖」登记入图。这是因为 include/patch 可能引用**尚未加载或尚未解析**的节点，必须延迟求解；而 append/merge 作用在已就绪的本地数据上，可以在求解当下立即执行。

#### 4.1.2 核心流程

YAML 被 yaml-cpp 解析成 `YAML::Node` 后，`ConvertFromYaml` 递归地把它翻译成 `ConfigItem` 树。当传入的 `compiler` 非空时，遍历到映射的每个键值对都会：

```
对每个 (key, value):
  compiler->Push(config_map, key)      # 把「当前节点位置」压栈
  child = ConvertFromYaml(value)       # 递归翻译子树
  compiler->Pop()
  if 编译器吃掉了这个 key（Parse 返回 true）:
      不写入 config_map   # __include/__patch 是指令，不作为普通键保留
  else:
      config_map.Set(key, child)       # 普通键正常写入
```

关键点：`__include` / `__patch` 这两个键**不会出现在最终的配置树里**——它们被 `Parse` 消费掉了。这就是为什么测试里到处断言 `config_->IsNull(prefix + "__include")`：展开后该位置是 `null`。

#### 4.1.3 源码精读

四类指令常量定义在类里（[src/rime/config/config_compiler.h:L43-L46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.h#L43-L46)）：

```cpp
static constexpr const char* INCLUDE_DIRECTIVE = "__include";
static constexpr const char* PATCH_DIRECTIVE = "__patch";
static constexpr const char* APPEND_DIRECTIVE = "__append";
static constexpr const char* MERGE_DIRECTIVE = "__merge";
```

`Parse` 是唯一的指令分发入口（[src/rime/config/config_compiler.cc:L545-L554](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L545-L554)）。它只认 `__include` 与 `__patch`，其余键原样返回 `false`（表示「不是指令，请当作普通键」）：

```cpp
bool ConfigCompiler::Parse(const string& key, const an<ConfigItem>& item) {
  if (key == INCLUDE_DIRECTIVE) return ParseInclude(this, item);
  if (key == PATCH_DIRECTIVE)   return ParseList(ParsePatch, this, item);
  return false;
}
```

`ParseInclude` 把字符串值（即目标路径）包成一个 `IncludeReference` 依赖登记入图（[src/rime/config/config_compiler.cc:L496-L505](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L496-L505)）：

```cpp
static bool ParseInclude(ConfigCompiler* compiler, const an<ConfigItem>& item) {
  if (Is<ConfigValue>(item)) {
    auto path = As<ConfigValue>(item)->str();
    compiler->AddDependency(New<IncludeReference>(compiler->CreateReference(path)));
    return true;
  }
  return false;
}
```

`ParsePatch` 更灵活：值既可以是字符串（引用另一处 patch 节点）、也可以是映射（字面量 patch 表）。`ParseList` 是一个适配器，让 patch 还能写成「字符串/映射的列表」，对每个元素分别调用 `ParsePatch`（[src/rime/config/config_compiler.cc:L510-L523](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L510-L523) 与 [src/rime/config/config_compiler.cc:L529-L543](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L529-L543)）：

```cpp
static bool ParsePatch(ConfigCompiler* compiler, const an<ConfigItem>& item) {
  if (Is<ConfigValue>(item)) {            // __patch: path/to/node
    compiler->AddDependency(New<PatchReference>(compiler->CreateReference(...)));
    return true;
  }
  if (Is<ConfigMap>(item)) {              // __patch: { key/a: v, key/b: v }
    compiler->AddDependency(New<PatchLiteral>(As<ConfigMap>(item)));
    return true;
  }
  return false;
}
```

而调用方 `ConvertFromYaml` 正是用 `Parse` 的返回值决定是否把键写入树（[src/rime/config/config_data.cc:L283-L285](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_data.cc#L283-L285)）：

```cpp
if (!compiler || !compiler->Parse(key, value)) {
  config_map->Set(key, value);   // 只有非指令键才真正写入
}
```

#### 4.1.4 代码实践

**目标**：验证「`__include` / `__patch` 是指令，展开后会从树中消失」。

**步骤**：

1. 打开测试数据 [data/test/config_compiler_test.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/test/config_compiler_test.yaml)，找到 `patch_literal` 段：

   ```yaml
   patch_literal:
     __patch:
       zerg/ground_units/@next: lurker
     zerg:
       __include: /starcraft/zerg
   ```

2. 对照测试 [test/config_compiler_test.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/config_compiler_test.cc) 中的 `PatchLiteral` 用例（约 L86-L95），注意第一行断言 `EXPECT_TRUE(config_->IsNull(prefix + "__patch"))`。

3. 运行该测试（若已按 u1-l2 构建了 `BUILD_TEST=ON`）：

   ```bash
   ./build/test/rime_test --gtest_filter='RimeConfigCompilerTest.PatchLiteral'
   ```

**需要观察的现象**：`__patch` 键读出来是 `null`，而它声明的修改（`zerg/ground_units` 多出一个 `lurker`）已经生效。

**预期结果**：测试通过；`zerg/ground_units/@5 == "lurker"`，列表长度为 6。若没有本地构建环境，此为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `__include` 误写成 `_include`（少一个下划线），会发生什么？

**答案**：`Parse` 对 `_include` 返回 `false`，于是它被当作普通键原样写入配置树，其字符串值成为该键的内容，不会触发任何 include。这也是为什么指令都带双下划线——降低与用户配置键撞名的概率。

**练习 2**：为什么 `__append` / `__merge` 不在 `Parse` 里分发？

**答案**：因为它们作用于「当前已就绪的本地节点」，不存在跨节点/跨文件的延迟求解问题，可以在节点编辑阶段（`EditNode`）按键名就地处理；而 `__include` / `__patch` 可能引用尚未加载的资源，必须先登记成依赖、留到链接阶段求解。

---

### 4.2 依赖图、依赖类型与优先级

#### 4.2.1 概念说明

`__include` / `__patch` 的本质是「当前节点的值依赖另一处节点的值」。由于依赖可能跨文件、可能尚未解析、甚至可能互相嵌套，编译器不能在 `Parse` 时就立刻求解，而是把每条依赖**登记入图**，等到链接阶段（`Link`）再统一求解。

依赖被抽象成 `Dependency` 基类，有四种具体类型（定义在 [src/rime/config/config_compiler_impl.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler_impl.h)）：

| 类型 | 由谁产生 | 优先级 | 含义 |
| --- | --- | --- | --- |
| `PendingChild` | 图构建过程自动派生 | `kPendingChild = 0` | 「我的某个子节点还没就绪，所以我暂时也不能被 include/patch」——非阻塞。 |
| `IncludeReference` | `__include` | `kInclude = 1` | 阻塞：必须先取到被引用节点。 |
| `PatchReference` | `__patch: 字符串` | `kPatch = 2` | 阻塞：必须先取到被引用的 patch 映射。 |
| `PatchLiteral` | `__patch: {映射}` | `kPatch = 2` | 阻塞：字面量 patch 表（数据已在手，但仍排在 include 之后求解）。 |

「优先级」决定了同一个节点上多条依赖的**求解顺序**。`InsertByPriority` 用 `std::upper_bound` 把新依赖按优先级升序插入，因此一个节点上的依赖始终排成：

\[ \text{kPendingChild}(0) \;\to\; \text{kInclude}(1) \;\to\; \text{kPatch}(2) \]

也就是说，**先等子节点就绪，再 include，最后 patch**。这一点至关重要：它意味着 YAML 里 `__include` 与 `__patch` 的书写先后**不影响**展开结果。

#### 4.2.2 核心流程

依赖图 `ConfigDependencyGraph` 内部维护四张表（[src/rime/config/config_compiler.cc:L24-L45](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L24-L45)）：

```
resources   : resource_id -> ConfigResource      // 已加载的资源
node_stack  : 当前正在遍历的节点引用栈（随 Push/Pop 变化）
key_stack   : 与 node_stack 对应的「路径段」栈
deps        : 节点路径 -> vector<Dependency>      // 每个节点上挂的依赖（按优先级排序）
resolve_chain: 当前正在求解的路径链（用于循环检测）
```

当 `Parse` 调用 `AddDependency` 时，`ConfigDependencyGraph::Add` 会：

1. 把依赖挂到「栈顶节点」（`node_stack.back()`）上，路径取自 `key_stack` 拼接。
2. 由于子节点的 include/patch 必须先于父节点的 include/patch 求解（否则父节点会拿到未展开的子树），还会向**所有尚未 pending 的祖先**插一条 `PendingChild` 依赖，把「pending 状态」向上传播。

这样，无论 include/patch 出现在多深的嵌套里，根节点最终都会带上整棵子树的 pending 标记，保证从根求解时能层层下沉、先内后外。

#### 4.2.3 源码精读

优先级枚举与基类（[src/rime/config/config_compiler_impl.h:L15-L32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler_impl.h#L15-L32)）：

```cpp
enum DependencyPriority {
  kPendingChild = 0,
  kInclude = 1,
  kPatch = 2,
};
struct Dependency {
  an<ConfigItemRef> target;                       // 依赖要作用到的目标节点
  virtual DependencyPriority priority() const = 0;
  bool blocking() const { return priority() > kPendingChild; }  // 非阻塞仅 kPendingChild
  virtual bool Resolve(ConfigCompiler* compiler) = 0;
  // ...
};
```

`blocking()` 的含义：阻塞依赖（include/patch）会拦住「穿过该节点向更深层访问」的请求（见 4.3）；而 `PendingChild` 不阻塞——它只是一个「请先求解我」的提示。

按优先级插入（[src/rime/config/config_compiler.cc:L280-L288](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L280-L288)）：

```cpp
static void InsertByPriority(vector<of<Dependency>>& list, const an<Dependency>& value) {
  auto upper = std::upper_bound(list.begin(), list.end(), value,
      [](const an<Dependency>& lhs, const an<Dependency>& rhs) {
        return lhs->priority() < rhs->priority();
      });
  list.insert(upper, value);   // 保持升序：PendingChild -> Include -> Patch
}
```

`ConfigDependencyGraph::Add` 的「向上传播 pending」逻辑（[src/rime/config/config_compiler.cc:L290-L326](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L290-L326)），核心是把当前节点挂上依赖后，再沿 `node_stack` 反向给每个尚未 pending 的祖先插一条 `PendingChild`：

```cpp
// Pending children should be resolved before applying __include or __patch
InsertByPriority(parent_deps,
                 New<PendingChild>(parent_path + "/" + last_key, *child));
```

#### 4.2.4 代码实践

**目标**：体会「优先级决定求解顺序，而非书写顺序」。

**步骤**：阅读 [data/test/config_dependency_test.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/test/config_dependency_test.yaml) 的 `dependency_priorities` 段：

```yaml
dependency_priorities:
  terrans:
    __include: starcraft:/terrans      # 文字上在前
    __patch:
      player: nada
  protoss:
    __patch:
      player: bisu
    __include: starcraft:/protoss      # 文字上在后
```

注意 `protoss` 把 `__patch` 写在了 `__include` **前面**。

**需要观察的现象**：无论书写顺序如何，两个分支最终都是「先 include 整个种族数据，再用 patch 覆盖 `player`」。

**预期结果**：对照 [test/config_compiler_test.cc:L130-L141](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/config_compiler_test.cc#L130-L141) 的 `DependencyPriorities` 断言：`terrans/player == "nada"`、`protoss/player == "bisu"`。若 patch 先于 include 执行，`player` 会被随后的 include 整体覆盖，拿不到这两个值——这反向证明了优先级排序生效。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PendingChild` 的优先级被刻意设成最低（0）？

**答案**：因为子节点比父节点「更内层」，必须最先求解。优先级越低越先被 `ResolveDependencies` 处理（遍历按列表升序），设成 0 保证 `PendingChild`（子节点未决）排在 `Include` / `Patch` 之前，从而实现「先内后外」。

**练习 2**：`blocking()` 为什么对 `kPendingChild` 返回 `false`？

**答案**：`PendingChild` 只是提示「我身上还有事没做完」，并不阻止别人读取我当前的（部分）值；而 include/patch 会**根本性地改写**当前节点的值，在它们求解完之前，别人读到的会是旧数据，所以必须阻塞。

---

### 4.3 依赖解析：阻塞、就绪与循环检测

#### 4.3.1 概念说明

登记完依赖图后，编译进入「链接」阶段（`ConfigCompiler::Link`）。链接不是一次性把整张图展开，而是**惰性**的：只有当某个节点被实际访问时，才按需求解它身上的依赖。这带来三个核心谓词：

- `resolved(path)`：该路径上没有依赖（或已全部求解），可以直接读。
- `pending(path)`：`!resolved(path)`，还有未决依赖。
- `blocking(path)`：该路径上**最后一条**依赖是阻塞型（include/patch）。阻塞节点在求解完成前，不允许越过它读取更深的子树——否则会读到未展开的旧数据。

「最后一条」之所以关键，是因为依赖按优先级升序排列，最后一条就是优先级最高的（通常是 `Patch`）。只要还有阻塞依赖在队尾，这个节点就处于「正在被改写」的状态。

#### 4.3.2 核心流程

求解的主循环在 `ResolveDependencies`（[src/rime/config/config_compiler.cc:L577-L600](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L577-L600)）：

```
ResolveDependencies(path):
  若 path 无依赖: 返回 true（已就绪）
  若 path 已在 resolve_chain 中（前缀匹配）: 检测到循环，警告并返回 false
  把 path 压入 resolve_chain
  遍历 deps[path]，逐条 Resolve；每条成功后立即从列表 erase
  弹出 resolve_chain
  返回 true
```

注意三点：

1. **惰性**：当外部代码（或 include/patch 自身）需要读取某个节点时，`GetResolvedItem` 会沿路径逐段调用 `ResolveBlockingDependencies`，遇到阻塞节点就就地求解，求解完才继续下钻。
2. **递归**：求解一条 `IncludeReference` 时，可能触发对被引用资源的 `Compile` 与进一步求解，形成递归。
3. **循环检测**：`resolve_chain` 记录「当前正在求解的路径栈」。若求解 A 时又要求解 A（或 A 的祖先/自身前缀），就构成循环，立即返回 false 并打 `WARNING`，避免无限递归。

#### 4.3.3 源码精读

三个谓词很简短（[src/rime/config/config_compiler.cc:L455-L468](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L455-L468)）：

```cpp
bool ConfigCompiler::blocking(const string& full_path) const {
  auto found = graph_->deps.find(full_path);
  return found != graph_->deps.end() && !found->second.empty() &&
         found->second.back()->blocking();   // 看队尾那条
}
bool ConfigCompiler::pending(const string& full_path) const { return !resolved(full_path); }
bool ConfigCompiler::resolved(const string& full_path) const {
  auto found = graph_->deps.find(full_path);
  return found == graph_->deps.end() || found->second.empty();
}
```

循环检测用「前缀匹配」判断 path 是否是 resolve_chain 中某条路径本身或其祖先（[src/rime/config/config_compiler.cc:L567-L575](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L567-L575)）：

```cpp
static bool HasCircularDependencies(ConfigDependencyGraph* graph, const string& path) {
  for (const auto& x : graph->resolve_chain) {
    if (boost::starts_with(x, path) &&
        (x.length() == path.length() || x[path.length()] == '/'))
      return true;   // x == path 或 x == path + "/..."
  }
  return false;
}
```

求解主循环（[src/rime/config/config_compiler.cc:L577-L600](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L577-L600)）：注意每条依赖 `Resolve` 成功后 `iter = deps.erase(iter)`，依赖被逐条「消费」掉，节点随之从 pending 变 resolved：

```cpp
graph_->resolve_chain.push_back(path);
auto& deps = found->second;
for (auto iter = deps.begin(); iter != deps.end();) {
  if (!(*iter)->Resolve(this)) {
    LOG(ERROR) << "unresolved dependency: " << **iter;
    return false;
  }
  iter = deps.erase(iter);   // 求解一条删一条
}
graph_->resolve_chain.pop_back();
```

#### 4.3.4 代码实践

**目标**：观察循环依赖被「尽力求解（best-effort）」而非崩溃。

**步骤**：阅读 [data/test/config_circular_dependency_test.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/test/config_circular_dependency_test.yaml)：

```yaml
test:
  __patch: sometimes?     # 引用 sometimes（带 ? 表示 optional）
  home: excited
  work:
    __include: /test/home   # work 引用同级的 home
sometimes:
  home: naive
```

这里 `test` patch 自 `sometimes`，而 `sometimes.home=naive`；`test.home` 本地值是 `excited`；`test.work` include `test.home`。

**需要观察的现象**：对照 [test/config_compiler_test.cc:L256-L266](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/config_compiler_test.cc#L256-L266) 的 `BestEffortResolution` 用例。

**预期结果**：`test/home == "excited"`、`test/work == "excited"`。编译器在日志里会打出 `circular dependencies detected` 的 `WARNING`，但**不崩溃**，已能确定的部分仍可读取。这是 librime 对错误配置的容错策略。若本地未构建，标为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`ResolveDependencies` 为什么在循环开头先检查「无依赖就返回 true」？

**答案**：因为大多数节点根本没有 include/patch，`deps` 里查不到该路径；直接返回 true 避免无谓的压栈与遍历，让惰性求解在「干净」节点上零开销。

**练习 2**：循环检测为什么用「前缀匹配」而非精确相等？

**答案**：因为依赖以路径串为键，父子节点的路径是前缀关系（如 `test:` 与 `test:/work`）。若求解 `test:` 时递归回到 `test:/work`，再回到 `test:`，精确相等也能命中；但前缀匹配能更稳健地覆盖「祖先链回指」等多种循环形态，统一用 `starts_with` 加边界判断（`x[len] == '/'`）避免误判同名前缀（如 `test` 与 `test2`）。

---

### 4.4 include 与 patch 的执行语义

#### 4.4.1 概念说明

知道「依赖如何排程」后，本模块看每条依赖**具体做什么**。先理解「引用路径」的写法——`CreateReference` 解析一个字符串得到 `Reference{resource_id, local_path, optional}`：

| 写法 | resource_id | local_path | 说明 |
| --- | --- | --- | --- |
| `starcraft` | 当前资源 | `starcraft` | 本文件内的 `starcraft` 节点 |
| `/starcraft` | 当前资源 | `/starcraft` | 同上（带根斜杠） |
| `:starcraft` | 当前资源 | `starcraft` | 同上（冒号开头表「本文件」） |
| `starcraft:/terrans` | `starcraft` | `/terrans` | 外部文件 `starcraft.yaml` 的 `/terrans` |
| `config_test:/` | `config_test` | `/` | 外部文件整棵根 |
| `sometimes?` | 当前资源 | `sometimes` | optional：找不到也不报错 |

末尾的 `?` 是 optional 标记：被引用资源/节点加载失败时，optional 引用返回成功（什么都不改），非 optional 引用则报 `ERROR` 并使求解失败。

四类依赖的 `Resolve` 行为：

- **`IncludeReference`**：取到被引用节点 `included`，先保存当前 target 上的「字面量兄弟键」`overrides`，再把 target 整体替换成 `included`，最后把 `overrides` 深合并回去。这支撑了「include + 同级写一些覆盖键」的惯用法。
- **`PatchReference`**：取到被引用节点（必须是映射），把它当作一张 patch 字面量表，委托 `PatchLiteral` 执行。
- **`PatchLiteral`**：遍历表里每条 `{路径: 值}`，调用 `EditNode` 对 target 做局部编辑（覆盖/追加/合并，见 4.5）。
- **`PendingChild`**：只是递归调用 `ResolveDependencies(child_path)`，确保子节点先就绪。

#### 4.4.2 核心流程

include 的执行（`IncludeReference::Resolve`）：

```
included = ResolveReference(reference)          # 取被引用节点（可能触发外部 Compile）
若 included 为空: 返回 reference.optional       # optional 容错
overrides = target 当前的字面量映射内容          # 保存同级兄弟键
*target = included                              # 整体替换
若 overrides 非空: MergeTree(target, overrides)  # 把兄弟键深合并回去
```

patch 字面量的执行（`PatchLiteral::Resolve`）：

```
对 patch 表里每条 (key, value):          # key 形如 "zerg/ground_units/@next"
    EditNode(target, key, value, merge_tree=false)
```

注意 `PatchLiteral` 调 `EditNode` 时 `merge_tree=false`，意味着 patch 默认是「按路径覆写」，除非键名带 `/+` 等后缀才转成追加/合并（见 4.5）。

取被引用节点的 `ResolveReference` 还会**按需触发外部资源编译**（[src/rime/config/config_compiler.cc:L475-L491](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L475-L491)）：若 `resource_id` 尚未加载，就 `compiler->Compile(resource_id)` 把那个文件也拉进来解析。

#### 4.4.3 源码精读

`CreateReference` 的字符串切分（[src/rime/config/config_compiler.cc:L336-L350](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L336-L350)），关键是 `?` 定 optional、`:` 切资源与本地路径、无 `:` 或 `:` 在首位则资源回退为当前资源：

```cpp
auto end = qualified_path.find_last_of("?");
bool optional = end != string::npos;
auto separator = qualified_path.find_first_of(":");
string resource_id = resource_resolver_->ToResourceId(
    (separator == string::npos || separator == 0)
        ? graph_->current_resource_id()          // 无冒号/冒号开头 -> 本文件
        : qualified_path.substr(0, separator));
string local_path = (separator == string::npos)
    ? qualified_path.substr(0, end)
    : qualified_path.substr(separator + 1, optional ? end - separator - 1 : end);
```

`IncludeReference::Resolve`（[src/rime/config/config_compiler.cc:L66-L80](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L66-L80)）：

```cpp
auto included = ResolveReference(compiler, reference);
if (!included) return reference.optional;        // optional 容错
auto overrides = As<ConfigMap>(**target);        // 保存同级兄弟键
*target = included;                              // 整体替换为被引用节点
if (overrides && !overrides->empty() && !MergeTree(target, overrides)) {
  LOG(ERROR) << "failed to merge tree: " << reference;
  return false;
}
return true;
```

`PatchLiteral::Resolve`（[src/rime/config/config_compiler.cc:L265-L278](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L265-L278)）逐条编辑：

```cpp
for (const auto& entry : *patch) {
  const auto& key = entry.first;            // 如 "zerg/ground_units/@next"
  const auto& value = entry.second;
  if (!EditNode(target, key, value, false)) {   // merge_tree=false
    LOG(ERROR) << "error applying patch to " << key;
    success = false;
  }
}
```

optional 的真实样例见 [data/test/config_optional_reference_test.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/test/config_optional_reference_test.yaml)，里面同时引用了三个不存在的资源，都带 `?`：

```yaml
__include: nonexistent.yaml:/?
__patch:
  - local/nonexistent_patch?
  - config_test:/nonexistent_patch?
  - nonexistent:/patch?
untouched: true
```

#### 4.4.4 代码实践

**目标**：用一个最小 YAML 把 `__include`（外部文件）+ `__patch`（字面量表）+ optional 串起来。

**步骤**：在 `data/test/` 下新建一个实验文件 `my_dsl_test.yaml`（示例代码，非项目原有文件）：

```yaml
# 示例代码：依赖 data/test/config_test.yaml 中的 starcraft 数据
__include: config_test:/
__patch:
  terrans/player: slayers_boxer          # 覆写标量
  protoss/air_force/@next: corsair       # 列表追加（@next = 末尾）
  zerg/missing?                          # optional：不存在也不报错
```

**需要观察的现象**：展开后，根节点等于整个 `config_test.yaml`，且 `terrans/player` 被改写、`protoss/air_force` 多了一项；`zerg/missing` 因 optional 静默忽略。

**预期结果**：若用 `ConfigComponent<ConfigBuilder>` 加载 `my_dsl_test`，应读到 `terrans/player == "slayers_boxer"`、`protoss/air_force` 末尾新增 `corsair`。由于这需要构建环境与测试 harness，标注「待本地验证」。可参照 [test/config_compiler_test.cc:L13-L26](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/config_compiler_test.cc#L13-L26) 的测试基类写法自行加一个 `TEST_F`。

#### 4.4.5 小练习与答案

**练习 1**：`IncludeReference::Resolve` 为什么要先保存 `overrides` 再替换、最后合并？

**答案**：为了支持「include 一个模板，同时在同级写几条覆盖」的惯用法（见 4.5 的 `merge_tree` 例子）。若直接替换，同级写的字面量键会被 include 的内容覆盖丢失；先存后合并，保证用户写的覆盖键能「叠加」到 include 进来的模板之上。

**练习 2**：`PatchReference`（字符串形式）与 `PatchLiteral`（映射形式）有何关系？

**答案**：`PatchReference` 先把被引用节点取回来并校验它是映射，然后构造一个 `PatchLiteral{那张映射}` 并委托它执行（见 [src/rime/config/config_compiler.cc:L82-L94](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L82-L94)）。二者最终都走 `PatchLiteral::Resolve` → `EditNode`，只是数据来源不同（引用 vs 字面量）。

---

### 4.5 节点编辑三态、后缀运算符与写时拷贝

#### 4.5.1 概念说明

无论 include 的「合并兄弟键」还是 patch 的「逐条编辑」，最终都落到一个核心函数 `EditNode` 上。它根据**键名**判断三种编辑语义：

1. **覆写（overwrite）**：默认行为，把目标路径的值整体替换。
2. **追加（append）**：键名为 `__append`、以 `/+` 结尾、或形如 `数字+`（如 `2+`）时触发——对字符串做拼接、对列表做插入。
3. **合并（merge）**：键名为 `__merge`、以 `/+` 结尾（非索引形式）、或在 `merge_tree=true` 上下文里遇到映射值时触发——对映射做深合并。

与之配套的「后缀运算符」：

| 写法 | 名称 | 语义 |
| --- | --- | --- |
| `key/+` | ADD 后缀 | 追加：列表末尾追加 / 字符串拼接 / 映射深合并 |
| `key/=` | EQU 后缀 | 强制覆写（即使在 merge_tree 上下文也覆写，不深合并） |
| `key/N+` | 索引追加 | 在列表的指定位置 N 处插入（如 `terrans/units/0+`） |
| `@next` / `@last` / `@before N` / `@after N` | 列表游标 | `@next`=末尾、`@last`=最后一项、`@before`/`@after`=插入点（见 `ResolveListIndex`） |

最后是**写时拷贝（COW）**：被 `__include` 进来的节点是**共享**的（多个方案可能 include 同一份模板）。当 `__patch` 修改其中一处时，绝不能污染原始模板。`ConfigCowRef<T>` 通过「写入前先复制父容器」实现这一点——只读时不复制，首次写入时沿路径把受影响的容器逐层复制一份，原始数据保持不变。

#### 4.5.2 核心流程

`EditNode` 的判定流水（[src/rime/config/config_compiler.cc:L233-L263](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L233-L263)）：

```
indexed_append = ParseIndexedAppend(key)     # 识别 "N+" 形式
appending = IsAppending(key, indexed_append) # __append / 结尾/+ / N+
merging  = IsMerging(key, value, merge_tree, indexed_append)
path = StripOperator(key, appending||merging, indexed_append)  # 去掉后缀得到纯路径
target = (merge_tree ? TypeCheckedCopyOnWrite : TraverseCopyOnWrite)(head, path)
if (appending 或 merging) 且 target 已有值:
    追加: AppendToString 或 AppendToList(target, ..., indexed_append)
    合并: MergeTree(target, value)
else:
    *target = value   # 覆写
```

COW 的写入路径（`ConfigCowRef::SetItem`，[src/rime/config/config_cow_ref.h:L24-L31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_cow_ref.h#L24-L31)）：

```
SetItem(item):
  container = parent_ 当前持有的容器
  if 未复制过:
      container = CopyOnWrite(container, key)   # 复制一份，替换 parent_ 的引用
      copied_ = true                            # 同一 ref 只复制一次
  Write(container, key, item)                   # 写入已复制的容器
```

`TraverseCopyOnWrite` 自上而下为路径每一层建一个 `ConfigCowRef`，链成一棵「延迟复制」的引用链；当最底层的写入发生时，`SetItem` 会逐层触发父容器的复制，从而把受影响的整条路径都复制成独立副本，共享的原始模板不受影响。

#### 4.5.3 源码精读

后缀常量与索引追加解析（[src/rime/config/config_compiler.cc:L165-L185](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L165-L185)）：

```cpp
static constexpr const char* ADD_SUFFIX_OPERATOR = "/+";
static constexpr const char* EQU_SUFFIX_OPERATOR = "/=";

static std::optional<size_t> ParseIndexedAppend(const string& key) {
  if (key.empty() || key.back() != '+') return std::nullopt;
  // ... 解析末尾的 "N+"，要求 N 前有 '/' 或位于串首
  return static_cast<size_t>(std::strtoull(index_part.c_str(), nullptr, 10));
}
```

三态判定（[src/rime/config/config_compiler.cc:L187-L202](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L187-L202)）：

```cpp
inline static bool IsAppending(const string& key, const std::optional<size_t>& indexed_append) {
  return key == ConfigCompiler::APPEND_DIRECTIVE ||
         boost::ends_with(key, ADD_SUFFIX_OPERATOR) ||
         indexed_append.has_value();
}
inline static bool IsMerging(const string& key, const an<ConfigItem>& value,
                             bool merge_tree, const std::optional<size_t>& indexed_append) {
  bool has_plain_add_suffix =
      boost::ends_with(key, ADD_SUFFIX_OPERATOR) && !indexed_append.has_value();
  return key == ConfigCompiler::MERGE_DIRECTIVE || has_plain_add_suffix ||
         (merge_tree && (!value || Is<ConfigMap>(value)) &&
          !boost::ends_with(key, EQU_SUFFIX_OPERATOR));   // /= 强制覆写
}
```

列表追加支持「把空映射节点转换成列表」与「在指定位置插入」（[src/rime/config/config_compiler.cc:L108-L143](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_compiler.cc#L108-L143)），核心是先 `New<ConfigList>(*existing_list)` 复制一份再 `Insert`/`Append`：

```cpp
auto copy = New<ConfigList>(*existing_list);          // 复制，不污染原列表
size_t current_index = insert_pos.value_or(copy->size());
for (...) {
  if (insert_pos) copy->Insert(current_index, *iter);  // N+ 在指定位置插入
  else            copy->Append(*iter);                 // /+ 或 __append 末尾追加
}
*target = copy;
```

COW 模板（[src/rime/config/config_cow_ref.h:L15-L54](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_cow_ref.h#L15-L54)），`CopyOnWrite` 在容器为空时新建、否则拷贝：

```cpp
template <class T>
inline an<T> ConfigCowRef<T>::CopyOnWrite(const an<T>& container, const string& key) {
  if (!container) { DLOG(INFO) << "creating node: " << key; return New<T>(); }
  DLOG(INFO) << "copy on write: " << key;
  return New<T>(*container);   // 拷贝构造一份独立副本
}
```

工厂函数 `Cow` 根据键名是列表项（`@...`）还是映射键，选择 `ConfigCowRef<ConfigList>` 或 `ConfigCowRef<ConfigMap>`（[src/rime/config/config_cow_ref.h:L82-L87](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_cow_ref.h#L82-L87)）：

```cpp
inline an<ConfigItemRef> Cow(an<ConfigItemRef> parent, string key) {
  if (ConfigData::IsListItemReference(key))
    return New<ConfigCowRef<ConfigList>>(parent, key);
  else
    return New<ConfigCowRef<ConfigMap>>(parent, key);
}
```

#### 4.5.4 代码实践

**目标**：用一条 `__include` + 多种 patch 后缀，验证「覆写 / 追加 / 合并」与 COW 不污染原数据。

**步骤**：阅读 [data/test/config_merge_test.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/test/config_merge_test.yaml) 的 `append_with_patch` 段，它一次性演示了三种 patch：

```yaml
append_with_patch:
  __include: starcraft
  __patch:
    terrans/player/+: ', nada'        # 字符串拼接（/+）
    terrans/air_units/+:              # 列表末尾追加（/+）
      - wraith
      - battlecruiser
    protoss/ground_units/+:           # 列表末尾追加（/+）
      - dark templar
      - dark archon
```

**需要观察的现象**：对照 [test/config_compiler_test.cc:L181-L204](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/config_compiler_test.cc#L181-L204) 的 `AppendWithPatch` 用例，特别注意末尾三行断言读取的是**原始** `/starcraft/...`：

```cpp
EXPECT_EQ("slayers_boxer", player);          // 原始 terrans/player 未被改
EXPECT_TRUE(config_->IsNull("/starcraft/terrans/air_units"));  // 原始无 air_units
EXPECT_EQ(6, config_->GetListSize("/starcraft/protoss/ground_units")); // 原始仍 6 项
```

**预期结果**：`append_with_patch/terrans/player == "slayers_boxer, nada"`（拼接成功）；其 `air_units` 有 2 项、`protoss/ground_units` 有 8 项；而原始 `starcraft` 下的对应数据**完全不变**——这就是 COW 的功劳。可运行：

```bash
./build/test/rime_test --gtest_filter='RimeConfigMergeTest.AppendWithPatch'
```

若未构建，标为「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`/+` 后缀对字符串、列表、映射分别是什么效果？

**答案**：对字符串做**拼接**（`AppendToString`，如 `player/+: ', nada'`）；对列表做**末尾追加**（`AppendToList` 无 `insert_pos`）；对映射做**深合并**（`IsMerging` 命中 `has_plain_add_suffix` 后走 `MergeTree`）。同一个后缀按目标类型自动选择语义。

**练习 2**：为什么测试要专门断言「原始 `/starcraft/...` 列表长度不变」？

**答案**：因为多个节点（`append_with_patch`、`merge_tree`、`patch_literal` 等）都 `__include` 了同一份 `starcraft` 数据，它们共享同一棵 `ConfigItem` 子树。若没有 COW，对其中一处做 `/+` 追加会污染所有共享者。断言原始数据不变，正是在验证 `ConfigCowRef` 的「写时拷贝」确实隔离了修改。

**练习 3**：`key/=（EQU 后缀）` 什么时候有用？

**答案**：在 `merge_tree=true` 的合并上下文里，遇到映射值默认会深合并；若用户希望**整体覆写**而非合并某个子键，就用 `/=` 强制走覆写分支（`IsMerging` 中 `!boost::ends_with(key, EQU_SUFFIX_OPERATOR)` 这一条件即为此而设）。

---

## 5. 综合实践

把本讲五个模块串起来，完成一个「迷你方案片段」的展开推演。

**任务**：阅读 [data/test/config_merge_test.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/test/config_merge_test.yaml) 的 `merge_tree` 段（L27-L48），它综合用到 `__include`（整体搬入）、同级字面量覆盖（触发 `IncludeReference` 的 `MergeTree`）、`__patch`（局部修改）、`/+`（列表追加）与覆写（`zerg/ground_units: []`）。请按下列步骤手动推演：

```yaml
merge_tree:
  __include: starcraft          # ① 整体 include starcraft
  terrans:                       # ② 同级字面量覆盖（深合并进 included 树）
    ground_units: [scv, marine, firebat, vulture, tank]
    __patch:                     # ③ 在合并前先 patch terrans
      ground_units/+: [medic, goliath]   # ④ /+ 追加
  protoss:
    ground_units: {__append: [dark templar, dark archon]}  # ⑤ __append 指令形式追加
  zerg:
    ground_units: []             # ⑥ 覆写：清空
```

**推演要点（请自行填空并对照测试 [test/config_compiler_test.cc:L206-L241](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/config_compiler_test.cc#L206-L241) 的 `MergeTree` 用例验证）**：

1. 节点 `merge_tree` 登记了一条 `IncludeReference(starcraft)` 依赖（优先级 1）；其子节点 `terrans` 因含 `__patch`，自身登记了 `PatchLiteral` 依赖（优先级 2），并向上把 `merge_tree` 标记为 pending。
2. 求解 `merge_tree` 时，先 include：`*target = starcraft 内容`，再把同级 `terrans`/`protoss`/`zerg` 三个兄弟键经 `MergeTree` 深合并进去（`EditNode(merge_tree=true)`）。
3. 合并 `terrans` 时，因其有 `__patch`，patch 先把 `ground_units/+` 追加（此时 `terrans.ground_units` 已是 include 来的 starcraft 值，但因 COW 不会被污染）。
4. `protoss.ground_units` 用 `__append` 指令追加两项；`zerg.ground_units` 直接被 `[]` 覆写为空列表。

**预期结果**（与 `MergeTree` 断言一致）：

- `merge_tree/terrans/ground_units` 长度 = \(5 + 2 = 7\)，`@5 == "medic"`。
- `merge_tree/protoss/ground_units` 长度 = \(6 + 2 = 8\)，`@6 == "dark templar"`。
- `merge_tree/zerg/ground_units` 长度 = 0（被覆写）。
- 原始 `/starcraft/protoss/ground_units` 仍为 6，`/starcraft/zerg/...` 仍为 5（COW 隔离）。

**进阶**：尝试把 `zerg/ground_units: []` 改成 `zerg/ground_units/=: []`，观察行为是否变化（应无变化，因为这里本就是覆写语义；体会 `/=` 主要在 merge_tree 想强制覆写时才有差别）。

---

## 6. 本讲小结

- `ConfigCompiler` 给 YAML 配置加了一层「DSL」：`__include` 整体搬入、`__patch` 局部修改、`__append` / `__merge`（以及 `/+` 后缀）做追加与深合并；这些指令键在展开后会从配置树中消失。
- include/patch 被建模成**依赖**登记入图（`ConfigDependencyGraph`），四种依赖类型按优先级排序求解：`kPendingChild(0) → kInclude(1) → kPatch(2)`，因此**求解顺序由优先级决定，与 YAML 书写顺序无关**。
- 求解是**惰性**的：`resolved` / `pending` / `blocking` 三个谓词控制访问；阻塞节点在求解完前不允许越过；`resolve_chain` 用前缀匹配检测循环依赖，出错时尽力求解而非崩溃。
- 引用路径有丰富写法：`name` / `/name` / `:name`（本文件）、`file:/path`（外部文件）、`path?`（optional）；`@next`/`@last`/`@before N`/`@after N` 是列表游标，`N+` 是索引追加。
- `EditNode` 是所有写操作的终点，按键名分**覆写 / 追加 / 合并**三态；`ConfigCowRef` 的写时拷贝保证对共享模板的 patch 不会污染原始数据。
- 本讲只讲了「编译器内核」；u4-l4 将讲 `ConfigBuilder` 挂载的配置插件族（`DefaultConfigPlugin` / `AutoPatchConfigPlugin` / `BuildInfoPlugin` 等）以及它们如何把 `import_preset` 转成 `__include`、如何在 `ReviewCompileOutput` / `ReviewLinkOutput` 时机介入。

---

## 7. 下一步学习建议

1. **继续配置系统**：进入 u4-l4（配置插件与 `default.yaml`），看 `legacy_preset_config_plugin` 如何把老式的 `import_preset: default` 翻译成本讲的 `__include`，把「DSL 内核」与「上层插件」接起来。
2. **动手做实验**：仿照 `test/config_compiler_test.cc` 的测试基类 `RimeConfigCompilerTestBase`（用 `ConfigComponent<ConfigBuilder>`），自己写一个 `TEST_F`，加载一份含 `__include` + `__patch` + `/+` 的小 YAML，用 GDB 在 `ConfigCompiler::ResolveDependencies` 与 `EditNode` 处下断点，观察依赖被逐条 `erase`、节点被 COW 复制的过程。
3. **回到运行时**：本讲都是构建期行为；如果想看展开后的配置如何驱动引擎，可跳到 u5（组件与模块）和 u6（按键处理流水线），看方案的 `engine/{processors,segmentors,translators,filters}` 四张清单如何被读取与装配。
4. **延伸阅读源码**：`src/rime/config/config_data.cc` 中的 `ResolveListIndex`（[L113-L153](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/config/config_data.cc#L113-L153)）实现了 `@next`/`@last`/`@before`/`@after` 游标的完整语义，值得单独精读，它是理解 4.5 中列表编辑的钥匙。
