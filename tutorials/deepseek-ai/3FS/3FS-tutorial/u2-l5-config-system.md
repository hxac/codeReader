# 配置系统与热更新

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 3FS 用「声明式宏 + 反射」定义配置项的套路，并能区分「可热更新」和「不可热更新」两类配置项。
- 解释一次配置热更新从「克隆试更新 → 逐项校验 → 原子提交 → 触发回调 → 整体比对」的完整链路，理解为什么它是「要么全成功、要么全不动」的原子语义。
- 理解运行时配置由 mgmtd 统一托管、带单调递增版本号、通过心跳下发给各服务的整体机制，并能用 `admin_cli` 的 `set-config` / `get-config` / `list-nodes` 完成一次热更新并验证生效。

本讲是公共基础设施单元（u2）的配置篇，承接 u2-l1（服务骨架）讲到的「三层配置（launcher / app / main）」与 `configPushable()`，向下为后续每个服务的配置项阅读打基础。

## 2. 前置知识

在进入源码前，先建立几个直觉。

**配置的三个生命周期阶段。** 一个 3FS 服务进程的配置分三段（详见 u1-l3、u2-l1）：

| 配置文件 | 加载阶段 | 是否热更新 | 作用 |
| --- | --- | --- | --- |
| `*_launcher.toml` | 启动前引导 | 否 | 指向 mgmtd、决定如何拼装节点身份 |
| `*_app.toml` | 引导期本地加载 | 否 | 节点身份（clusterId / nodeId 等） |
| `*_main.toml` | 运行时 | 是 | 由 mgmtd 托管，可在线变更 |

本讲主要关心第三类 `*_main.toml`：它在 mgmtd 里以版本号管理，运行期可改。我们以 [`configs/storage_main_app.toml`](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/configs/storage_main_app.toml#L1-L2) 为例——它只有两行 `allow_empty_node_id` 和 `node_id`，这正是「`*_app.toml` 只放节点身份、不放运行时参数」的体现；真正的运行时参数在 `storage_main.toml`，由 mgmtd 托管。

**什么是「声明式配置」。** 传统写法是在结构体里手动写字段、再手动写解析与序列化代码。3FS 用宏做到「一行声明，同时生成字段、默认值、类型反射、TOML 序列化、校验钩子」。声明即文档，这也是为什么本讲大量出现的不是「字段定义」，而是宏。

**什么是「热更新」。** 不重启进程、不中断服务，在运行期把某个参数（如超时、并发度、阈值）换成新值，并让正在运行的代码立刻读到新值。难点在于并发：其它线程可能正读着旧值，你不能在它读到一半时把值改掉。

**热更新的安全边界。** 不是所有参数都能热更新。比如「监听端口」「线程数」这类在启动时就申请了资源的参数，改了也只在重启后生效——这类参数必须显式标记为「不可热更新」，强行在线改会被框架拒绝。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/common/utils/ConfigBase.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/ConfigBase.h#L1-L926) | 配置框架核心：声明宏、`Item`、`ConfigBase`、线程安全存储、校验器、回调 |
| [src/common/app/ConfigManager.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ConfigManager.h#L1-L38) / [ConfigManager.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ConfigManager.cc#L1-L113) | 热更新的高层编排：渲染、试更新、比对、状态机（NORMAL/DIRTY/FAILED） |
| [src/common/app/ApplicationBase.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ApplicationBase.h#L1-L76) / [ApplicationBase.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ApplicationBase.cc#L1-L284) | 应用层入口：加载本地配置文件、暴露 `updateConfig`/`hotUpdateConfig` 给 RPC 调用 |
| [src/common/app/ConfigStatus.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ConfigStatus.h#L1-L7) | 配置状态枚举 |
| [src/mgmtd/ops/SetConfigOperation.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/SetConfigOperation.cc#L1-L61) | mgmtd 收到 set-config 后落库 + 版本号推进 |
| [src/client/mgmtd/MgmtdClient.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L728-L740) | 服务进程通过心跳检测到新配置版本，回调 `updateConfig` 把配置「拉」下来 |
| [src/common/net/IOWorker.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/IOWorker.h#L25-L36) / [src/storage/worker/AllocateWorker.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/worker/AllocateWorker.h#L16-L22) | 真实可热更新配置项的样例 |
| [src/client/cli/admin/SetConfig.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/SetConfig.cc#L33-L67) / [GetConfig.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/GetConfig.cc#L33-L175) | admin_cli 的 set-config / get-config 命令 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**配置声明**（怎么用宏定义配置项）、**热更新**（运行期怎么安全地换值并触发回调）、**配置下发**（mgmtd 怎么把配置托管、推进版本并分发给各服务）。

### 4.1 配置声明：ConfigBase 框架与宏

#### 4.1.1 概念说明

3FS 的所有配置类都继承自 `ConfigBase<Self>`（CRTP 奇异递归模板）。一个配置「类」由两类成员组成：

- **item（配置项）**：一个带类型的值，例如超时 `1_s`、布尔 `true`。用 `CONFIG_ITEM` / `CONFIG_HOT_UPDATED_ITEM` 声明。
- **section（配置节）**：一个嵌套的子配置对象，对应 TOML 里的一个 `[section]` 表。用 `CONFIG_OBJ` / `CONFIG_SECT` 声明。

框架的精妙之处在于：声明宏在**构造时**就把「字段名 → 成员指针」登记到一张反射表里（`items_` / `sections_`）。于是同一个 `ConfigBase` 类既知道自己的字段（用于 TOML 解析/序列化），又能在运行期按字符串名查找字段（用于增量热更新「只改这一项」）。

每个 item 还带两个属性：

1. **默认值**——没显式配置时的取值。
2. **是否支持热更新**——`CONFIG_ITEM` 默认 `false`，`CONFIG_HOT_UPDATED_ITEM` 为 `true`。
3. （可选）**校验器 checker**——一个 lambda，返回 `false` 则拒绝该值。

#### 4.1.2 核心流程

声明一个配置类的流程，用伪代码表示：

```
struct Config : ConfigBase<Config> {
  CONFIG_HOT_UPDATED_ITEM(tcp_connect_timeout, 1_s);   // 可热更新，默认 1 秒
  CONFIG_ITEM(num_event_loop, 1u);                      // 不可热更新，默认 1
  CONFIG_OBJ(ibsocket, IBSocket::Config);               // 嵌套子配置节
};
```

构造对象时，每个宏展开后都会执行一个立即调用的 lambda（`[this](){...}()`），把自己登记进反射表：

- `CONFIG_ADD_ITEM` 登记进 `items_[#name]`，并记录 `supportHotUpdated`。
- `CONFIG_OBJ` 登记进 `sections_[#name]`。

之后无论「从 TOML 文件整体加载」还是「热更新只改一个字段」，都靠这张反射表分发。

读取值时，`item.name()` 返回当前值；由于值存放在线程安全的 `StoreType<T>` 里（小类型用 `AtomicValue`，大类型用 `TLSStore`，见 4.2），任何线程都能无锁读到最新值。

#### 4.1.3 源码精读

**两个核心声明宏**——一行同时声明成员、默认值、反射登记与「是否热更新」标记：

[src/common/utils/ConfigBase.h:115-116](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/ConfigBase.h#L115-L116) 区分可热更新与否（`CONFIG_HOT_UPDATED_ITEM` 只是 `CONFIG_ADD_ITEM(..., true, ...)` 的别名）：

```cpp
#define CONFIG_ITEM(name, defaultValue, ...) CONFIG_ADD_ITEM(name, defaultValue, false, __VA_ARGS__)
#define CONFIG_HOT_UPDATED_ITEM(name, defaultValue, ...) CONFIG_ADD_ITEM(name, defaultValue, true, __VA_ARGS__)
```

[src/common/utils/ConfigBase.h:96-113](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/ConfigBase.h#L96-L113) 是 `CONFIG_ADD_ITEM` 的展开——注意它生成了 `name()` 取值、`set_##name()` 设值，并在初始化 lambda 里把成员指针塞进 `items_[#name]`，同时把 `supportHotUpdated` 作为构造参数传给 `Item`：

```cpp
::hf3fs::config::Item<T##name> name##_ = ::hf3fs::config::Item<T##name>(#name, defaultValue, [this] {
    using Self = std::decay_t<decltype(*this)>;
    ConfigBase<Self>::items_[#name] = reinterpret_cast<...>(&Self::name##_);
    return supportHotUpdated;   // true / false
}() __VA_OPT__(, ) __VA_ARGS__)   // 可选的 checker lambda 接在后面
```

**item 的统一接口** `IItem`——每个配置项都是它的实现，提供 `validate`、`update`、`toToml`、`toString` 等虚函数：

[src/common/utils/ConfigBase.h:129-137](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/ConfigBase.h#L129-L137) `update` 的第二个参数 `isHotUpdate` 就是「这次更新算不算热更新」的开关：

```cpp
struct IItem {
  virtual Result<Void> validate(const std::string &path) const = 0;
  virtual Result<Void> update(const toml::node &node, bool isHotUpdate, const std::string &path) = 0;
  virtual void toToml(toml::table &table) const = 0;
  ...
};
```

**真实配置项样例**——网络层 IOWorker 的配置，混用了两类：

[src/common/net/IOWorker.h:25-36](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/IOWorker.h#L25-L36) 中 `tcp_connect_timeout`（`1_s`）、`rdma_connect_timeout`（`5_s`）、`wait_to_retry_send`（`100_ms`）都是 `CONFIG_HOT_UPDATED_ITEM`，而 `num_event_loop`（事件循环线程数）是 `CONFIG_ITEM`——线程数在启动时已固定分配，故不允许热改：

```cpp
class Config : public ConfigBase<Config> {
  CONFIG_HOT_UPDATED_ITEM(tcp_connect_timeout, 1_s);
  CONFIG_HOT_UPDATED_ITEM(rdma_connect_timeout, 5_s);
  CONFIG_HOT_UPDATED_ITEM(wait_to_retry_send, 100_ms);
  CONFIG_ITEM(num_event_loop, 1u);          // ← 不可热更新
  CONFIG_OBJ(ibsocket, IBSocket::Config);
  ...
};
```

这里的 `1_s`、`100_ms` 是用户自定义字面量，定义在 [src/common/utils/Duration.h:44-45](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/Duration.h#L44-L45)，把整数包成强类型的 `Duration`。

另一个样例在存储分配器：[src/storage/worker/AllocateWorker.h:16-22](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/worker/AllocateWorker.h#L16-L22)，`min_remain_groups` / `max_remain_groups` 等都是可热更新的预留阈值。

**内置校验器**——`CONFIG_ITEM`/`CONFIG_HOT_UPDATED_ITEM` 的可选最后参数是一个 checker lambda。框架在 [src/common/utils/ConfigBase.h:861-911](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/ConfigBase.h#L861-L911) 预备了一批，例如 `checkPositive`（必须 > 0）、`checkNotNegative`（必须 ≥ 0）、`checkNotEmpty`（容器非空）、`checkGE<T, threshold>`（必须 ≥ 阈值）。

#### 4.1.4 代码实践

**实践目标：** 从源码层面识别「哪些配置项能热更新、哪些不能」，理解宏的差别。

**操作步骤（源码阅读型）：**

1. 打开 [src/common/net/IOWorker.h:25-36](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/IOWorker.h#L25-L36)。
2. 把该 `Config` 里的每个字段列成三列：字段名 / 默认值 / 是否热更新。
3. 思考：为什么 `num_event_loop` 不能热更新，而 `tcp_connect_timeout` 可以？（提示：前者在构造时就 `eventLoopPool_(config_.num_event_loop())` 固定了线程池大小；后者只是一个被连接逻辑每次读取的超时值。）

**需要观察的现象 / 预期结果：**

- 用 `CONFIG_HOT_UPDATED_ITEM` 声明的字段 → 「可热更新」。
- 用 `CONFIG_ITEM` 声明的字段 → 「不可热更新」，运行期改它会被框架拒绝（详见 4.2）。

#### 4.1.5 小练习与答案

**练习 1：** 如果你想新增一个「最大重试次数」配置项，要求必须 ≥ 1 且能在线调整，该怎么写？

> **参考答案：** `CONFIG_HOT_UPDATED_ITEM(max_retry, 3u, checkPositive);` —— `true` 表示可热更新，`3u` 是默认值，`checkPositive`（来自 ConfigCheckers）保证值 > 0。

**练习 2：** 一个配置类同时被「整体加载文件」和「热更新单字段」使用，框架靠什么把字符串名（如 `"tcp_connect_timeout"`）映射到具体成员？

> **参考答案：** 靠声明宏在构造时填入的反射表 `items_`（`std::map<std::string, IItem Parent::*>`）。键是字段名字符串，值是指向成员的指针，运行期用 `reinterpret_cast` 转换后调用 `Item::update`。

---

### 4.2 热更新：原子更新、回调与校验

#### 4.2.1 概念说明

热更新要解决两个问题：

1. **并发安全**：其它线程可能在读配置，更新不能撕裂。
2. **原子性**：一次下发的新配置里可能有 N 个字段，要么 N 个都改成功，要么一个都不改——不能改了一半留下一个「半新半旧」的配置。

3FS 的解法分两层：

- **低层（`StoreType`）**：每个 item 的值存放在线程安全容器里。小类型（≤8 字节、trivially copyable，如 int/bool/Duration）用 `AtomicValue` 原子变量；大类型（如 vector/string/嵌套对象）用 `TLSStore`——一个原子 shared_ptr + 每线程缓存的版本号。读路径无锁，写路径整体替换指针并 bump 版本号。
- **高层（`atomicallyUpdate`）**：先 `clone()` 一份当前配置当「草稿」，在新草稿上跑一遍 `update`（含校验和热更新门禁）；只要有一项失败就立即返回错误、真实配置毫发无损。草稿全部通过后，才对真实配置再跑一次同样的 `update`。这就是「原子」的来源。

此外，配置更新后往往需要「副作用」——例如改了某个超时后要重建连接池、改了阈值后要唤醒等待线程。框架用 `ConfigCallbackGuard` 提供「更新后回调」机制：把回调登记进 `callbacks_` 集合，每次 `ConfigBase::update` 成功后统一触发。

#### 4.2.2 核心流程

一次热更新的高层时序（伪代码）：

```
hotUpdateConfig(片段)
  → renderConfig(模板)           # 目前是直通（预留的模板渲染钩子）
  → cfg.atomicallyUpdate(片段)   # 原子更新
       ├─ clone() 一份草稿 newConfig
       ├─ newConfig.update(片段) # 在草稿上试：逐项校验 + 热更新门禁
       │     若任一项失败 → 直接返回错误，真实配置未动
       └─ update(片段)           # 试通过了，对真实配置正式更新
            └─ 逐项 Item::update → setValue（原子替换）
            └─ for each callback: callCallback()   # 触发副作用
            └─ overallValidate()                   # 跨字段整体校验
  → 更新 ConfigStatus（见 4.2.4）
```

**热更新门禁**是核心安全机制。一个 item 是否允许在「这次调用」里被改，取决于一个布尔：

\[
\text{enableUpdate} \;=\; \text{supportHotUpdate} \;\lor\; \lnot\,\text{isHotUpdate}
\]

含义：要么该字段本身就支持热更新（`supportHotUpdate = true`），要么这次调用根本不是热更新（`isHotUpdate = false`，例如启动时加载文件）。两者皆不满足时，框架拒绝该次修改并返回错误。真值表：

| `supportHotUpdate` | `isHotUpdate`（本次调用是否算热更新） | `enableUpdate`（允许改？） |
| --- | --- | --- |
| true | true | ✅ 允许（典型热更新） |
| true | false | ✅ 允许（启动加载） |
| false | true | ❌ 拒绝（不允许热改） |
| false | false | ✅ 允许（启动加载，可改不可热更新项） |

#### 4.2.3 源码精读

**门禁实现**就在 `Item::update` 里：

[src/common/utils/ConfigBase.h:409-422](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/ConfigBase.h#L409-L422) 计算门禁并按类型分发：

```cpp
Result<Void> update(const toml::node &node, bool isHotUpdate, const std::string &path) final {
  bool enableUpdate = supportHotUpdate_ || !isHotUpdate;   // ← 门禁公式
  if constexpr (is_vector_v<T> || is_set_v<T>) return updateVectorOrSet(...);
  else if constexpr (is_map_v<T>)                 return updateMap(...);
  else                                            return updateNormal(...);
}
```

[src/common/utils/ConfigBase.h:459-479](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/ConfigBase.h#L459-L479) 普通类型的更新——先解析、再跑 checker、再比对：值没变就跳过；变了但门禁关闭则返回「Not support hot update」错误：

```cpp
if (res.value() != value()) {
  if (enableUpdate) {
    setValue(std::move(res.value()));
  } else {
    return makeError(..., fmt::format("Not support hot update: {}", path));   // ← 拒绝
  }
}
```

**原子更新**——先克隆试跑，通过后才正式改：

[src/common/utils/ConfigBase.h:739-745](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/ConfigBase.h#L739-L745)：

```cpp
Result<Void> atomicallyUpdate(std::string_view str, bool isHotUpdate) final {
  Parent newConfig = clone();                       // 草稿
  RETURN_ON_ERROR(newConfig.update(str, isHotUpdate));  // 试跑：校验 + 门禁
  auto res = update(str, isHotUpdate);              // 正式改
  XLOGF_IF(FATAL, !res, "Unexpected update error: {}", res.error());  // 试过还失败=逻辑bug
  return Void{};
}
```

注意：草稿 `newConfig` 是栈上副本，试跑失败时它直接析构，真实 `*this` 完全未动。

**更新后回调**——`ConfigBase::update` 末尾统一触发：

[src/common/utils/ConfigBase.h:633-637](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/ConfigBase.h#L633-L637)：

```cpp
// call callbacks after update.
for (auto &callback : callbacks_) {
  callback->callCallback();
}
return overallValidate();
```

回调靠 [src/common/utils/ConfigBase.h:782-787](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/ConfigBase.h#L782-L787) 的 `addCallbackGuard` 登记，返回一个 RAII guard，析构时自动注销。

**真实副作用样例**——IOWorker 在构造时给 `ibsocket` 配置节挂了回调，改了 `drop_connections` 就立刻断开所有连接重建：

[src/common/net/IOWorker.h:49-55](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/IOWorker.h#L49-L55)：

```cpp
ibsocketConfigGuard_(config.ibsocket().addCallbackGuard([this] {
  auto newVal = config_.ibsocket().drop_connections();
  if (dropConnections_.exchange(newVal) != newVal) {
    XLOGF(INFO, "ioworker@{} drop all connections", fmt::ptr(this));
    dropConnections();        // ← 副作用：热更新触发的连接重建
  }
}))
```

**线程安全存储 TLSStore**——大类型用「原子指针 + 版本号 + 线程局部缓存」实现无锁读：

[src/common/utils/ConfigBase.h:268-276](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/ConfigBase.h#L268-L276) 读路径：每个线程缓存一份版本号，发现全局版本号变了才重新 load 指针，否则直接用本地缓存，避免每次读都走原子 load：

```cpp
const T &value() const {
  auto &cache = *tlsCache_;
  size_t latest = version_.load(std::memory_order_acquire);
  if (UNLIKELY(cache.version != latest)) {   // 版本变了才刷新
    cache.ptr = ptr_.load(std::memory_order_acquire);
    cache.version = latest;
  }
  return *cache.ptr;
}
```

#### 4.2.4 代码实践（ConfigManager 状态机）

**实践目标：** 理解热更新后的「配置状态」如何从 NORMAL 变成 DIRTY/FAILED，并学会读 `list-nodes` 里的 ConfigVersion 列。

热更新的高层入口在 `ConfigManager`。它不仅要更新值，还要判定「这次更新到底有没有让运行配置和托管配置保持一致」，并把结果记成 `ConfigStatus`：

[src/common/app/ConfigStatus.h:6](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ConfigStatus.h#L6) 定义了五种状态：

```cpp
enum class ConfigStatus : uint8_t { NORMAL = 0, DIRTY = 1, FAILED = 2, UNKNOWN = 3, STALE = 4 };
```

三种常见状态的含义：

- **NORMAL**：运行配置与 mgmtd 托管配置完全一致（diff 为 0）。
- **DIRTY**：两者存在差异——典型原因是托管配置里改了某个**不可热更新**字段，运行时改不动，等下次重启才会生效。
- **FAILED**：本次更新直接报错（渲染失败 / 解析失败 / 校验失败），运行配置未被改动。

核心判定逻辑在 `updateConfigContent`：

[src/common/app/ConfigManager.cc:42-64](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ConfigManager.cc#L42-L64)——它用一个「默认构造 + 同样内容更新」得到的 `expected` 配置，与运行中的真实配置做 `diffWith`，有差异就 DIRTY，否则 NORMAL：

```cpp
auto expected = cfg.defaultPtr();
auto updateRes = expected->atomicallyUpdate(std::string_view(*renderRes), /*isHotUpdate=*/false);  // 期望态
if (updateRes.hasError()) { configStatus = ConfigStatus::FAILED; return; }

if (checkDiff) {
  config::IConfig::ItemDiff diffs[10];
  auto diffCnt = cfg.diffWith(*expected, std::span(diffs));   // 真实 vs 期望
  if (diffCnt != 0) { ... configStatus = ConfigStatus::DIRTY; }   // 有差异
  else              { configStatus = ConfigStatus::NORMAL; }
}
```

**操作步骤（源码阅读型）：**

1. 读 [src/common/app/ConfigManager.cc:71-90](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ConfigManager.cc#L71-L90) 的 `updateConfig`：它先 `renderConfig`、再 `atomicallyUpdate`（真正的热更新发生在这里，带 `hotUpdate` 门禁）、再 `updateConfigContent`（判定 NORMAL/DIRTY）、最后记录一条 `ConfigUpdateRecord`。
2. 对照上面的门禁真值表回答：如果托管配置改了一个 `CONFIG_ITEM`（不可热更新）字段，`atomicallyUpdate` 会怎样？状态会是什么？

**预期结果：**

- 改不可热更新字段 → `atomicallyUpdate(..., hotUpdate=true)` 会在该项返回 `Not support hot update` 错误，整个更新被拒，运行配置不变，状态 **FAILED**。

> 说明：`updateConfig` 的默认 `hotUpdate=true`。如果你想让 mgmtd 在下发时「能改就改、不能改的留待重启」，那是另一条路径（DIRTY），需要区分调用入口；本实践聚焦默认行为。**待本地验证** FAILED vs DIRTY 在不同 set-config 场景下的具体表现。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `atomicallyUpdate` 要先在 `clone()` 上试跑一遍，而不是直接改真实配置？

> **参考答案：** 为了原子性。若直接改真实配置，改到第 3 个字段时发现校验失败，前 2 个已经改了，配置就处于「半新半旧」的不一致状态。先在草稿上跑，全部通过才动真实配置，保证「要么全改、要么不动」。

**练习 2：** ConfigStatus 的 DIRTY 和 FAILED 分别在什么情况下出现？

> **参考答案：** FAILED 是本次更新本身报错（渲染/解析/校验/门禁失败），运行配置完全未变。DIRTY 是更新没报错但运行配置和托管配置存在差异——最常见是托管配置里含不可热更新字段，运行期改不动，需重启才生效。

---

### 4.3 配置下发：mgmtd 托管、版本推进与服务拉取

#### 4.3.1 概念说明

前面两节讲的是「单个进程内」如何更新配置。本节回答：**新配置从哪来、怎么到达每个运行中的进程？**

3FS 的答案是**集中托管 + 心跳拉取**（与 u1-l3、u1-l4 呼应）：

- **托管**：mgmtd 是配置的唯一权威来源。它把**每种 NodeType**（MGMTD / META / STORAGE / CLIENT）的运行时配置存在 FoundationDB 里，用单调递增的 `ConfigVersion` 标识每次变更。注意是「按类型」而非「按节点」——同类型的所有节点共享同一份配置。
- **下发**：不是 mgmtd 主动推，而是各服务进程在**心跳**时附带自己当前的 configVersion，mgmtd 比对后若有更新就把新配置内容塞进心跳回包；服务收到后回调本进程的 `ApplicationBase::updateConfig` 完成热更新。

这套机制的关键好处：mgmtd 不需要维护「哪些节点在线、要推给谁」的推送状态，下发完全由心跳这一既有机制顺带完成；配置变更的版本号也天然和心跳的租约续期绑在一起。

#### 4.3.2 核心流程

管理员改配置的端到端链路：

```
admin_cli: set-config -t STORAGE -f new.toml --desc "..."
   │  读取本地 TOML 文件内容
   ▼
mgmtdClient.setConfig(type, content, desc)
   │  RPC → MgmtdService
   ▼
mgmtd: SetConfigOperation::handle                          # 必须 primary 才能处理
   ├─ 从 configMap 取该 type 的最新版本 oldVersion
   ├─ newVersion = nextVersion(oldVersion)                 # 版本号 +1
   ├─ storeConfig 写入 FoundationDB (type, newVersion, content, desc)
   ├─ 内存 configMap[type][newVersion] = ConfigInfo
   └─ 若 type==MGMTD：本地立即热更新自身 + 更新 selfNodeInfo.configVersion
   ▼  返回 newVersion 给 admin_cli
（此后下一次各 STORAGE 节点心跳）
storage 进程心跳: hbReq 带上自己当前 configVersion
   ▼
mgmtd 心跳回包: 发现节点版本落后 → 回包里带上最新 ConfigInfo(content)
   ▼
storage 的 MgmtdClient 收到 res->config:
   ├─ 回调 serverConfigListener_(content, version)   # 即 ApplicationBase::updateConfig
   │     ├─ 检查 configPushable()
   │     ├─ renderConfig + atomicallyUpdate (4.2)
   │     └─ onConfigUpdated()
   └─ 成功后把本地 configVersion 记成新版本（失败则保持旧版本，下次心跳还会收到）
```

注意最后一步的容错：**只有当 listener 返回成功，本地 configVersion 才会推进**；若热更新失败，版本号不变，下一次心跳 mgmtd 还会重发——这就实现了「可靠送达、失败重试」的语义。

#### 4.3.3 源码精读

**配置的数据结构** `ConfigInfo`——就是带版本号的内容容器：

[src/fbs/mgmtd/ConfigInfo.h:7-14](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/mgmtd/ConfigInfo.h#L7-L14)：

```cpp
struct ConfigInfo : public serde::SerdeHelper<ConfigInfo> {
  SERDE_STRUCT_FIELD(configVersion, ConfigVersion(0));
  SERDE_STRUCT_FIELD(content, String{});
  SERDE_STRUCT_FIELD(desc, String{});
  String genUpdateDesc() const { return fmt::format("version: {} desc: {}", ...); }
};
```

**mgmtd 侧：set-config 落库 + 推版本**——

[src/mgmtd/ops/SetConfigOperation.cc:14-35](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/SetConfigOperation.cc#L14-L35) 在写锁内取旧版本、算新版本、写 FDB、更新内存：

```cpp
auto writerLock = co_await state.coScopedLock<"SetConfig">();
auto oldVersionRes = ... it->second.rbegin()->first;     // 该 type 当前最新版本
auto newVersion = nextVersion(*oldVersionRes);            // +1
auto newConfigInfo = flat::ConfigInfo::create(newVersion, content, req.desc);
// 写入 FoundationDB
co_await state.store_.storeConfig(txn, nodeType, newConfigInfo);
// 更新内存 configMap
dataPtr->configMap[nodeType][newVersion] = std::move(newConfigInfo);
```

`nextVersion` 就是简单的 +1（[src/mgmtd/service/helpers.h:16-20](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/helpers.h#L16-L20)），整个操作包在 `doAsPrimary` 里——**只有 primary mgmtd 能改配置**（多实例选主见 u3-l3）。

**mgmtd 自己也是被配置的对象**——当 `type == MGMTD` 时，primary 收到自己类型的新配置，会立即热更新自身，并把自身节点的 configVersion 推进：

[src/mgmtd/ops/SetConfigOperation.cc:39-55](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/ops/SetConfigOperation.cc#L39-L55)（通过 `env_->configUpdater()`，即 `ApplicationBase::updateConfig`）：

```cpp
if (nodeType == flat::NodeType::MGMTD) {
  const auto &updater = state.env_->configUpdater();     // = ApplicationBase::updateConfig
  if (!updater || updater(content, newConfigInfo.genUpdateDesc())) {
    state.selfNodeInfo_.configVersion = newVersion;      // 推进自身版本
    ...
  }
}
```

`configUpdater` 在 mgmtd 启动时被注入为 `ApplicationBase::updateConfig`：[src/mgmtd/MgmtdServer.cc:29-30](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/MgmtdServer.cc#L29-L30)：

```cpp
env->setConfigUpdater(ApplicationBase::updateConfig);
env->setConfigValidater(ApplicationBase::validateConfig);
```

**meta/storage 侧：通过心跳被动拉取**——服务启动时把自己的 `updateConfig` 注册成「配置监听器」：

[src/meta/service/MetaServer.cc:39](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/service/MetaServer.cc#L39)：

```cpp
mgmtdClient_->setConfigListener(ApplicationBase::updateConfig);
```

心跳回包里若带 `res->config`，就回调这个监听器，**成功才推进本地版本号**：

[src/client/mgmtd/MgmtdClient.cc:731-740](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L731-L740)（服务端节点路径）：

```cpp
if (res->config) {
  auto listener = serverConfigListener_.load(std::memory_order_acquire);
  // do not update config version when listener return false
  if (!listener || (*listener)(res->config->content, fmt::format("{}", res->config->configVersion))) {
    heartbeatInfo_->configVersion = res->config->configVersion;   // 成功才推进
  }
}
```

**应用层入口**——`updateConfig` 做了 `configPushable()` 检查，然后委托给 ConfigManager，成功后回调 `onConfigUpdated()`：

[src/common/app/ApplicationBase.cc:115-133](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ApplicationBase.cc#L115-L133)：

```cpp
Result<Void> ApplicationBase::updateConfig(const String &configContent, const String &configDesc) {
  auto lock = std::unique_lock(appMutex);
  if (!globalApp || !globalApp->getConfig()) { ... kNoApplication ... }
  if (!globalApp->configPushable()) { ... kCannotPushConfig ... }   // 该 app 声明不可热推送
  auto res = getConfigManager().updateConfig(configContent, configDesc, *globalApp->getConfig(), globalApp->info());
  if (res) { globalApp->onConfigUpdated(); }                        // 成功后的钩子
  return res;
}
```

**渲染钩子 renderConfig**——目前是直通函数，但保留了「按 AppInfo 做模板替换」的位置：

[src/common/utils/RenderConfig.cc:71-75](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/RenderConfig.cc#L71-L75)：

```cpp
Result<String> renderConfig(const String &configTemplate, const flat::AppInfo *app, ...) {
  return configTemplate;   // 当前直接返回原文；预留为按节点信息渲染模板的扩展点
}
```

这意味着现阶段「同类型节点共享完全相同的配置文本」，没有按节点差异化渲染。

**admin_cli 命令**——`set-config` 读取本地文件、按类型上传：

[src/client/cli/admin/SetConfig.cc:52-66](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/SetConfig.cc#L52-L66)：

```cpp
auto loadFileRes = loadFile(*path);                                  // 读 TOML 文件
auto res = co_await env.mgmtdClientGetter()->setConfig(env.userInfo, t, *loadFileRes, desc);
table.push_back({"ConfigVersion", std::to_string(*res)});            // 返回新版本号
```

类型映射见 [src/client/cli/admin/SetConfig.cc:13-20](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/SetConfig.cc#L13-L20)，`STORAGE` / `META` / `MGMTD` / `FUSE` 等对应 `flat::NodeType`。

#### 4.3.4 代码实践（端到端热更新验证）

**实践目标：** 用 admin_cli 对一个 storage 配置项做一次真正的热更新，并验证它在线生效（不重启进程）。

**操作步骤：**

1. **导出当前托管配置**（参考 [src/client/cli/admin/GetConfig.cc:48-69](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/GetConfig.cc#L48-L69)）：

   ```bash
   admin_cli "get-config -t STORAGE -o storage.toml"
   ```

   得到 mgmtd 当前托管的、带版本号的完整 storage 配置。

2. **挑一个可热更新字段修改。** 例如存储分配器的预留组阈值（见 [src/storage/worker/AllocateWorker.h:17-18](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/worker/AllocateWorker.h#L17-L18) 的 `min_remain_groups` / `max_remain_groups`），在 `storage.toml` 里把它的值改一下。具体它在 TOML 里的层级路径（如 `allocate_worker.min_remain_groups`）**待本地确认**——可对照导出的文件结构。

3. **上传新配置**（参考 [src/client/cli/admin/SetConfig.cc:33-39](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/SetConfig.cc#L33-L39)）：

   ```bash
   admin_cli "set-config -t STORAGE -f storage.toml --desc 'bump min_remain_groups'"
   ```

   命令应返回新的 `ConfigVersion`（见 [SetConfig.cc:63-65](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/SetConfig.cc#L63-L65)）。

4. **等待一个心跳周期，验证下发到位。** 用 u1-l3 学过的：

   ```bash
   admin_cli "list-nodes -t STORAGE"
   ```

   观察每个 storage 节点的 **ConfigVersion** 列：应从旧版本推进到新版本，状态显示 **UPTODATE**（即 ConfigStatus=NORMAL）；若显示 **DIRTY** 说明有字段改不动（可能误改了不可热更新字段）。

5. **验证在线生效。** 观察该字段对应的运行行为是否即时变化，例如 storage 日志里 `Update config succeeded`（[ConfigManager.cc:87](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ConfigManager.cc#L87)），或对应 worker 的实际预留组数变化。

**需要观察的现象：**

- `set-config` 返回的 `ConfigVersion` 比 `get-config` 看到的旧版本大 1。
- 心跳间隔后，`list-nodes` 中节点 ConfigVersion 推进、状态 UPTODATE，**整个过程无重启**。
- 若故意改一个 `CONFIG_ITEM`（不可热更新）字段：`set-config` 本身可能仍返回新版本（mgmtd 已落库），但节点状态会变 DIRTY，运行值不变，需重启才生效。**待本地验证** 这两种情形的确切输出。

**预期结果：** 可热更新字段在线生效、状态 NORMAL/UPTODATE；不可热更新字段标记 DIRTY、待重启。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 3FS 选择「按 NodeType 托管一份配置」而非「每个节点一份」？

> **参考答案：** 同类型的所有节点职责相同、配置本就该一致，按类型托管能避免 N 份重复配置的管理开销和版本漂移；同时配合 `renderConfig`（当前直通）保留了未来按节点差异化渲染的余地。

**练习 2：** 如果某次心跳时 storage 节点的 `updateConfig` 回调失败了（比如新配置校验不通过），本地 configVersion 会推进吗？后果是什么？

> **参考答案：** 不会推进（见 [MgmtdClient.cc:733-739](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/mgmtd/MgmtdClient.cc#L733-L739)，listener 返回 false 就不更新版本）。后果是 mgmtd 认为该节点仍是旧版本，下一次心跳会**再次**下发同一份新配置，直到节点成功应用——实现了可靠送达与失败重试。

---

## 5. 综合实践

把三个模块串起来，完成一次「诊断式」配置排查任务。

**场景：** 你通过 `set-config -t STORAGE -f new.toml` 改了 storage 配置，但发现某个行为没变化。请按以下步骤定位问题：

1. **查托管版本**：`admin_cli "get-config -l"`（见 [GetConfig.cc:147-172](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/client/cli/admin/GetConfig.cc#L147-L172)）列出各 type 的最新 ConfigVersion，确认 set-config 确实推进了版本。

2. **查节点状态**：`admin_cli "list-nodes -t STORAGE"`，看节点 ConfigVersion 是否跟上、状态是 UPTODATE 还是 DIRTY。

3. **若是 DIRTY**：说明你改的字段不可热更新。回到源码 [IOWorker.h:25-36](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/net/IOWorker.h#L25-L36) / [AllocateWorker.h:16-22](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/worker/AllocateWorker.h#L16-L22)，确认该字段是 `CONFIG_ITEM`（不可热更新）还是 `CONFIG_HOT_UPDATED_ITEM`。若是前者，需重启节点才生效。

4. **若是 FAILED**：查 storage 日志中的 `Update config failed`（[ConfigManager.cc:79-85](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ConfigManager.cc#L79-L85)）和 `Config has diffs`（[ConfigManager.cc:53-57](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/app/ConfigManager.cc#L53-L57)），定位是解析失败、校验失败（checker 返回 false）还是热更新门禁拒绝（`Not support hot update`，[ConfigBase.h:472](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/ConfigBase.h#L472)）。

5. **画出完整链路图**：在你的笔记里画出从 `set-config` → `SetConfigOperation` → FDB → 心跳回包 → `MgmtdClient` listener → `ApplicationBase::updateConfig` → `ConfigManager::updateConfig` → `atomicallyUpdate` → `Item::update`（门禁）→ 回调 → `onConfigUpdated` 的全链路，标注每一步可能失败的位置和对应的 ConfigStatus。

完成本实践后，你应能在不重启服务的前提下，准确判断一次配置变更「到底生效了没有、为什么没生效」。

## 6. 本讲小结

- **配置声明靠宏**：`CONFIG_ITEM` / `CONFIG_HOT_UPDATED_ITEM` 一行声明字段+默认值+热更新标记+（可选）校验器，构造时自动登记进反射表，同一份类既支持整体加载也支持按字段热更新。
- **热更新门禁**：`enableUpdate = supportHotUpdate || !isHotUpdate`——不可热更新字段在热更新调用里会被拒绝，保护了「启动时申请资源的参数」。
- **原子语义**：`atomicallyUpdate` 先克隆试跑（校验+门禁），全过才正式改，保证「要么全改、要么不动」；值用 `AtomicValue`/`TLSStore` 无锁安全读取。
- **更新后回调**：`addCallbackGuard` 登记的回调在每次 `update` 成功后统一触发，用于连接重建、唤醒等待等副作用。
- **集中托管+心跳拉取**：mgmtd 按 NodeType 托管配置、用单调递增 `ConfigVersion` 标识变更；服务进程在心跳回包里被动收到新配置，回调 `updateConfig` 应用，**成功才推进本地版本**，失败则下次心跳重发。
- **状态机 NORMAL/DIRTY/FAILED**：NORMAL 表示运行配置与托管配置一致；DIRTY 表示有差异（通常因不可热更新字段）；FAILED 表示本次更新报错。`list-nodes` 的 ConfigVersion 列是验证下发是否到位的直观指标。

## 7. 下一步学习建议

- **深入具体服务的配置**：本讲只读了网络层和分配器的配置样例。建议带着「哪些字段可热更新」的视角去读 [src/storage/service/Components.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/storage/service/Components.h)、[src/meta/base/Config.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/base/Config.h)、[src/mgmtd/service/MgmtdConfig.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/mgmtd/service/MgmtdConfig.h)，为 u3/u4/u5 各服务总览做准备。
- **mgmtd 的版本与路由体系**：本讲的 `ConfigVersion` 是 mgmtd 管理的众多版本号之一。后续 u3-l1（RoutingInfo 数据模型）和 u3-l6（路由信息分发）会讲 `RoutingInfoVersion` 等同源机制，建议对照阅读，理解 mgmtd「所有变更都带版本号、靠版本号驱动各方拉取」的统一设计。
- **FDB 事务**：`SetConfigOperation` 把配置写入 FoundationDB 的那一步（`storeConfig` + `withReadWriteTxn`）涉及事务，u2-l6 会专门讲 FDB 客户端与事务封装，学完后可回看本讲的 set-config 落库过程。
- **动手扩展**：尝试给某个服务新增一个 `CONFIG_HOT_UPDATED_ITEM` 配置项（带 checker），重新构建后用 `set-config` 验证它真能在线生效——这是检验你是否真正理解本讲的最好方式。

---

本讲义覆盖的最小模块：**配置声明（ConfigBase 宏与反射）、热更新（原子更新/门禁/回调/状态机）、配置下发（mgmtd 托管、ConfigVersion 推进、心跳拉取）**。
