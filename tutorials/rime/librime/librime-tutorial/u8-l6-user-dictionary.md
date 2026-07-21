# 用户词典与 user_db

## 1. 本讲目标

u8-l5 讲的是**静态词典**（`.table.bin`）：它由 `*.dict.yaml` 在构建期编译出来，运行期只读不改。但真实的输入法还会「学习」——你越打某个词，它就越靠前；你能造出码表里没有的新词；你不小心上错了词，还能删掉。这些动态行为都由**用户词典**承担。

本讲要回答四个问题：

> 1. 用户的学习数据存在哪里？用什么数据结构？
> 2. 用户敲一个词上屏时，引擎怎么「记住」它、怎么更新它的权重？
> 3. 用户词典和静态词典一样要做「音节图 → 候选」的查询，它的查询路径有什么不同？
> 4. 数据库如果崩溃损坏了怎么办？同步（sync）到另一台设备时发生了什么？

读完本讲，你应当能够：

- 说清 `UserDbValue`（`commits` / `dee` / `tick`）三个字段的含义，以及 `'code\tword'` 这条键的构造与解析。
- 画出「一次提交 → `Memory::OnCommit` → `UserDictionary::UpdateEntry` → `commits++`、`tick++`、重算 `dee`、写回 LevelDB」的完整链路。
- 理解 `Db` 抽象接口（`Fetch`/`Update`/`Query`）及其 `Transactional` / `Recoverable` 两个混入（mixin），以及 `LevelDb` 与 `TextDb` 两种后端。
- 描述 `UserDictionary::Lookup` 的 DFS 查询（区别于 `Dictionary::Lookup` 的 BFS）与 `CreateDictEntry` 的对数空间权重公式。
- 解释 3 秒内按退格键「撤销上次提交」、`sync_user_data` 同步、以及 `userdb_recovery_task` 崩溃恢复三件事各自的机制。

本讲是 u8-l5 的姊妹篇：`UserDictionary` 沿用了几乎相同的 `map<size_t, Iterator>` collector 模型，但底层从 mmap 码表换成了 LevelDB 键值存储，且会**动态更新权重**。学完两讲，你会清楚地看到「静态查询」与「动态学习」两套机制的对称与差异。

## 2. 前置知识

本讲默认你已经掌握以下内容（对应前置讲义）：

- **`Dictionary` 查询主链路**（u8-l5）：`DictEntryCollector = map<size_t, DictEntryIterator>` 按 `end_pos` 分桶；候选权重工作在**自然对数空间**，公式 `weight = e.weight - log(1e8) + credibility`；`DictEntry` 的字段（`text`/`code`/`weight`/`commit_count`/`custom_code`/`remaining_code_length` 等）。
- **`SyllableGraph`（音节图）**（u7-l3）：尤其它的 `indices` 表（`位置 → (syllable_id → 属性列表)` 的转置邻接表）——本讲的 DFS 查询正是沿这张表走。
- **`Code` 与 `SyllableId`**（u8-l3/u8-l5）：`Code` 是 `vector<SyllableId>`，`SyllableId` 是音节在 `Table` 内部 `Syllabary`（音节表）里的下标。
- **组件 / Registry 体系**（u5-l1）：`Class<T, Arg>` 模板、`Component<T>` 默认工厂、`Require(name)` 按名取工厂。
- **Engine 提交信号**（u3-l1/u2-l4）：`Context` 的 `commit_notifier_` 在一段输入上屏时被触发。

两个贯穿全讲的关键概念：

- **`tick`（滴答）**：一个单调递增的 `uint64_t` 计数器，本质上是「这台用户词典自创建以来累计被成功提交了多少次」。它是用户词典内部的「逻辑时钟」——所有带时间衰减的权重计算都以它为时间轴，而非墙钟（wall clock）。它持久化在数据库的元数据键 `/tick` 里。
- **`dee`（dynamic efficacy estimate，动态效能估计）**：每个用户词条维护的一个浮点数，刻画「这个词最近有多常被用到」。它是一个带指数衰减的累加器，下面 4.3 会用公式讲清。

再回顾一条工程惯例：**对数空间**。和静态词典一样，用户词典的候选权重也取对数，让「概率相乘」退化为「对数相加」。本讲会看到 `formula_d` / `formula_p` 两个衰减公式，它们定义在 [src/rime/algo/dynamics.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/dynamics.h)，是理解权重更新的数学核心。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/rime/dict/db.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/db.h) | `Db` 抽象基类、`DbAccessor` 迭代器、`Transactional`/`Recoverable` 混入、`DbComponent` 工厂模板 |
| [src/rime/dict/db.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/db.cc) | `Db::CreateMetadata` 基类实现（写 `/db_name`、`/rime_version`） |
| [src/rime/dict/level_db.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/level_db.h) / [level_db.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/level_db.cc) | `LevelDb`：基于 Google LevelDB 的默认用户库后端，实现事务与 `RepairDB` 恢复 |
| [src/rime/dict/text_db.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/text_db.h) | `TextDb`：纯文本（TSV）后端，可读性好、用于跨格式备份与测试 |
| [src/rime/dict/user_db.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db.h) / [user_db.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db.cc) | `UserDbValue`（学习数据）、`UserDbWrapper`/`UserDbComponent`（把任意 `Db` 包装成用户库）、`UserDbMerger`（合并） |
| [src/rime/dict/user_dictionary.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.h) / [user_dictionary.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc) | 本讲主角 `UserDictionary`：DFS 查询、`UpdateEntry` 权重更新、事务、`CreateDictEntry` |
| [src/rime/algo/dynamics.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/dynamics.h) | `formula_d`（带衰减的累加）、`formula_p`（频率 × 效能 → 优先级） |
| [src/rime/gear/memory.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/memory.h) / [memory.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/memory.cc) | `Memory`：把「提交」事件翻译成「记忆」的中间层，连接 `Context` 信号与 `UserDictionary` |
| [src/rime/dict/user_db_recovery_task.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db_recovery_task.h) / [user_db_recovery_task.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db_recovery_task.cc) | `UserDbRecoveryTask`：数据库损坏时的部署期恢复任务 |
| [test/user_db_test.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/user_db_test.cc) | 用户库的增删查测试，是本讲实践的依据 |

---

## 4. 核心概念与源码讲解

### 4.1 Db 抽象接口与 LevelDb / TextDb 后端

#### 4.1.1 概念说明

用户词典需要一块**可随机读写、可持久化、按键排序遍历**的存储。librime 没有把它绑死在某一种存储引擎上，而是先抽象出一个 `Db` 接口，再提供两种具体后端：

- **`LevelDb`**：基于 Google 的 [LevelDB](https://github.com/google/leveldb) 嵌入式键值库，是**默认后端**（注册名 `userdb`），高效、二进制、支持事务。
- **`TextDb`**：纯文本 TSV（制表符分隔）存储，注册名 `plain_userdb`，人类可读、便于跨设备同步与离线编辑，也是测试里用的后端。

`Db` 抽象层的好处是：上层 `UserDictionary` 完全不关心底层是 LevelDB 还是文本文件，它只调用 `Fetch`/`Update`/`Query` 等纯虚接口。换后端只需在方案 YAML 里改一个 `db_class` 字段。

`Db` 还通过两个**混入类（mixin）**声明可选能力：

- `Transactional`：支持事务（`BeginTransaction`/`AbortTransaction`/`CommitTransaction`）。用户词典用它实现「3 秒内退格撤销上次提交」。
- `Recoverable`：支持 `Recover()`。用户词典用它实现「数据库损坏时尝试修复」。

后端类用多继承「领养」这些能力：`class LevelDb : public Db, public Recoverable, public Transactional`。

#### 4.1.2 核心流程

`Db` 的接口可以分成四组：

```
生命周期：  Open / OpenReadOnly / Close / Exists / Remove
元数据：    CreateMetadata / MetaFetch / MetaUpdate / QueryMetadata
数据读写：  Fetch(key) / Update(key,val) / Erase(key)
范围查询：  Query(prefix) / QueryAll()  —— 返回 DbAccessor 迭代器
备份恢复：  Backup(snapshot_file) / Restore(snapshot_file)
```

这里有个精巧的设计：**元数据与数据共用同一套键值对，靠「键的前缀字节」区分**。以 LevelDb 为例，所有元数据键前面都加一个 `\x01` 字节（`kMetaCharacter`）：

[src/rime/dict/level_db.cc:17](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/level_db.cc#L17) 定义了 `kMetaCharacter = "\x01"`。

[src/rime/dict/level_db.cc:295-301](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/level_db.cc#L295-L301) `MetaFetch`/`MetaUpdate` 实际上就是在键前拼一个 `\x01` 再调 `Fetch`/`Update`。

为什么用 `\x01`？因为 LevelDB（和大多数有序键值库）按字节序排列键。用户数据键是 `'code\tword'`（如 `"ni hao \t你好"`），以字母开头（`'n'` = 0x6e）；而 `'\x01'`（= 1）比空格（0x20）和所有字母都小。于是**所有元数据键天然排在所有数据键之前**。遍历全部数据时只要 `Jump(" ")`（跳到第一个 ≥ 空格的键）就能干净地跳过整段元数据：

[src/rime/dict/level_db.cc:160-165](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/level_db.cc#L160-L165) `QueryAll` 在 `Query("")` 之后追加一句 `all->Jump(" ")` 跳过元数据。`UserDictionary::Lookup`（见 4.3）也用了同样的 `Jump(" ")` 技巧。

事务的实现依赖 LevelDB 的 `WriteBatch`：事务期间所有写操作先攒进批次、不落盘，提交时一次性写入；中止时清空批次即可，已攒的改动全部丢弃。

#### 4.1.3 源码精读

先看 `Db` 抽象基类与 `DbAccessor`：

[src/rime/dict/db.h:33-73](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/db.h#L33-L73) 定义了 `Db` 类，注意它继承 `Class<Db, const string&>`——这意味着 `Db` 本身是一个**组件**，组件名就是「db 类名」（如 `userdb`、`plain_userdb`），用方案里的 `db_class` 字段选择。第 49-57 行那一串纯虚函数就是上面四组接口。

[src/rime/dict/db.h:16-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/db.h#L16-L31) `DbAccessor` 是范围查询的游标：`Jump(key)` 定位、`GetNextRecord` 顺序读、`MatchesPrefix` 做前缀过滤。它是 4.3 里 `UserDictionary::Lookup` 逐键扫描的基础。

[src/rime/dict/db.h:75-92](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/db.h#L75-L92) `Transactional` 与 `Recoverable` 两个混入。`Transactional` 的三个虚函数默认都返回 `false`（即「不支持」），由真正支持事务的后端覆写。

接着看基类的 `CreateMetadata`：

[src/rime/dict/db.cc:54-58](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/db.cc#L54-L58) 打开一个新库时，先写两条元数据：`/db_name`（库名）和 `/rime_version`（创建它的 librime 版本）。子类会在此基础上追加自己的元数据（如 LevelDb 追加 `/db_type`）。

再看 LevelDb 的事务实现——这是本模块最值得读的部分：

[src/rime/dict/level_db.cc:72-79](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/level_db.cc#L72-L79) `LevelDbWrapper::Update(key, value, write_batch)`：事务进行中（`write_batch=true`）只往 `batch` 里 `Put`，不真正写库；非事务才直接 `ptr->Put`。

[src/rime/dict/level_db.cc:179-184](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/level_db.cc#L179-L184) `LevelDb::Update` 把「是否在事务中」传给底层：`db_->Update(key, value, in_transaction())`。`Erase` 同理（第 186-191 行）。

[src/rime/dict/level_db.cc:303-326](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/level_db.cc#L303-L326) 三步：`BeginTransaction` 清空批次并置标志位；`AbortTransaction` 清空批次（丢弃已攒改动）；`CommitTransaction` 调 `CommitBatch` 一次性写入。这正是「3 秒内退格撤销」得以实现的底层基础——撤销就是 `AbortTransaction`。

[src/rime/dict/level_db.cc:218-227](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/level_db.cc#L218-L227) `Recover()` 直接调用 LevelDB 自带的 `leveldb::RepairDB`，尝试修复损坏的库文件。这是 4.4 里恢复任务的第一道防线。

最后看一眼 `TextDb` 后端：

[src/rime/dict/text_db.h:40-76](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/text_db.h#L40-L76) `TextDb` 用内存里的 `map<string,string>`（`TextDbData`）持有全部键值对，打开时从 TSV 文件加载、关闭时写回。它没有继承 `Transactional`/`Recoverable`，所以不支持事务与修复——但因为它纯文本、可读，被用作跨格式备份的「统一快照格式」（见 4.4 的 `UniformBackup`）。`test/user_db_test.cc` 里用的就是它。

#### 4.1.4 代码实践

**实践目标**：用 `test/user_db_test.cc` 验证 `Db` 接口的 `Update/Fetch/Erase/Query` 四件套，并亲手观察 `\x01` 元数据前缀的排序效果。

**操作步骤**：

1. 打开 [test/user_db_test.cc:16-41](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/user_db_test.cc#L16-L41) 的 `AccessRecordByKey` 测试。注意第 14 行 `using TestDb = UserDbWrapper<TextDb>;`——测试用文本后端，无需真正安装 LevelDB。
2. 阅读断言：`Update("zyx", "CBA")` 后再 `Update("zyx", "ABC")`，`Fetch` 出来是 `"ABC"`（覆盖语义，第 25/31 行）；`Erase("zyx")` 之后 `Fetch` 返回 `false`（第 36-37 行）。
3. 打开 [test/user_db_test.cc:43-91](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/user_db_test.cc#L43-L91) 的 `Query` 测试。第 74 行 `db.Query("wvu\tt")` 因为没有以 `"wvu\tt"` 为前缀的键，`accessor->exhausted()` 立即为真——这印证了 `DbAccessor` 的前缀过滤。
4. 想观察 `\x01` 前缀：在一个 `TestDb` 上调 `db.Open()` 后，用 `db.QueryMetadata()`（或直接 `Query("\x01")`）遍历，应该能看到 `/db_name`、`/rime_version`、`/user_id`、`/tick` 等元数据键，而 `QueryAll()`（`Jump(" ")` 之后）只返回数据键、不含元数据。

**需要观察的现象**：元数据键在 `Query("")`（从头遍历）时排在最前面；`QueryAll()` 与 `UserDictionary::Lookup` 用 `Jump(" ")` 之后，迭代器恰好跳过所有元数据、从第一条数据键开始。

**预期结果**：两个测试均通过；元数据与数据靠键的前缀字节天然分区，无需额外索引。

> 若想实际运行：构建时确保 `BUILD_TEST=ON`（u1-l2），执行 `ctest -R user_db_test`。**待本地验证**运行环境；不运行时，断言本身就是行为规格。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Db` 用 `\x01` 作为元数据前缀，而不是用一个独立的命名空间（比如 `meta/tick`）？

**参考答案**：键值库（LevelDB）只提供「按字节序的范围扫描」。用 `'\x01'`（最小的可见字节之一）作前缀，让所有元数据键天然排在所有数据键（以字母开头）之前，形成一个连续的元数据区。这样既能在 `Query("\x01")` 时只取元数据，也能在 `Jump(" ")` 时一次跳过整段元数据，无需维护额外的索引或分隔符。`meta/tick` 这种命名虽然也能排序，但 `'m'` 排在数据键中间，会和真实数据键交织，无法用一次 `Jump` 干净分区。

**练习 2**：`LevelDb` 的事务在「中止」时做了什么？为什么这样做是安全的？

**参考答案**：`AbortTransaction` 只是 `db_->ClearBatch()`（清空 `WriteBatch`）并清标志位（[level_db.cc:311-317](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/level_db.cc#L311-L317)）。因为事务期间所有写操作都只进了 `WriteBatch`、从未真正 `ptr->Put` 落盘，清空批次就等于这些改动从未发生过，磁盘上的库文件保持事务前的状态，因此是安全的。这也意味着事务期间崩溃至多丢失「这次未提交的改动」，不会留下半写状态。

---

### 4.2 UserDb 与 UserDbValue：用户学习数据的存储格式

#### 4.2.1 概念说明

4.1 讲的是「通用键值库」。本模块讲「用户词典往这个库里**存什么**」。

一条用户词条在库里是**一个键值对**：

- **键（key）**：`'code\tword'`——编码串 + 制表符 + 词条文本。例如拼音 `"ni hao \t你好"`（编码串里每个音节后跟一个空格）。编码串用空格分隔音节，是为了让键按字典序排列时，「同音节前缀」的词条聚在一起——这正是 4.3 里 DFS 前缀扫描能高效工作的前提。
- **值（value）**：一个 `UserDbValue`，打包成字符串 `"c=<commits> d=<dee> t=<tick>"`。三个字段记录这条词的学习历史：

| 字段 | 类型 | 含义 |
|------|------|------|
| `commits` | `int` | 这条词被成功上屏的次数。负值表示「被用户标记删除」（软删除，不立即抹除）。 |
| `dee` | `double` | 动态效能估计（dynamic efficacy estimate）——带指数衰减的「近期使用度」累加器，见 4.3。 |
| `tick` | `TickCount`(`uint64_t`) | 这条词**最后一次被更新时**的全局 tick。用于计算衰减（距离现在过了多少 tick）。 |

这套键值格式与 `.dict.yaml` 里的 `text\tcode\tweight` 列格式（u8-l1、u8-l4）相似但不同：用户库把 code 放前、text 放后，是为了让键按「编码」排序聚簇。

`UserDbValue` 的 `Pack()`/`Unpack()` 负责字符串与结构体的互转。值得注意一个细节：`Unpack` 解析 `dee` 时会 `std::min(10000.0, ...)` 截断（[user_db.cc:41](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db.cc#L41)），防止异常大的 `dee` 值（比如从损坏的旧库读入）污染权重。

#### 4.2.2 核心流程

`UserDb` 不是某个具体后端，而是一个**占位类（placeholder）**：它本身不能实例化（构造函数 `= delete`），只提供一组与「用户库」相关的静态/嵌套工具——`UserDbValue`、`UserDbHelper`、`UserDbWrapper` 模板、`UserDbComponent` 模板。真正的用户库对象是 `UserDbWrapper<LevelDb>` 或 `UserDbWrapper<TextDb>`。

整体装配关系：

```
方案 YAML:  db_class: userdb   (或 plain_userdb)
                 │  Registry::Require
                 ▼
        UserDbComponent<LevelDb>            (工厂，dict_module.cc 注册)
                 │  Create(dict_name)
                 ▼
        UserDbWrapper<LevelDb>  ──is-a──▶  LevelDb ──is-a──▶ Db
                 │  (多了 CreateMetadata/Backup/Restore 的用户库特化)
                 ▼
        UserDictionary 持有 an<Db> db_
```

两个模板各司其职：

- **`UserDbWrapper<BaseDb>`**：在任意 `Db` 后端之上，覆写三个方法——`CreateMetadata`（创建时额外写 `/user_id`）、`Backup`/`Restore`（如果快照是统一的 `.userdb.txt` 文本格式，走 `UniformBackup`；否则退回后端自有实现）。它就是把「通用 Db」打扮成「用户库」的装饰器。
- **`UserDbComponent<BaseDb>`**：对应的工厂，`Create(name)` 算出文件路径（`name + extension()`）并 `new UserDbWrapper<BaseDb>(...)`。`extension()` 由模板特化提供：`LevelDb` 后端是 `.userdb`，`TextDb` 后端是 `.userdb.txt`。

`dict_module.cc` 在 `dict` 模块初始化时注册了两个后端：

[src/rime/dict/dict_module.cc:30-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_module.cc#L30-L31) `plain_userdb` → `UserDbComponent<TextDb>`，`userdb` → `UserDbComponent<LevelDb>`。方案里不写 `db_class` 时默认用 `userdb`（见 4.3.3 的 `UserDictionaryComponent::Create`）。

#### 4.2.3 源码精读

先看 `UserDbValue` 的打包与解析：

[src/rime/dict/user_db.h:20-30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db.h#L20-L30) 三个字段加上 `Pack`/`Unpack` 声明。

[src/rime/dict/user_db.cc:22-52](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db.cc#L22-L52) `Pack` 拼成 `"c=.. d=.. t=.."`；`Unpack` 按空格切分、按 `=` 拆键值，用 `try/catch` 包住 `std::stoi/stod/stoul`，任一字段解析失败就记 ERROR 并返回 `false`。这种「宽松解析 + 容错」是为了兼容旧版本或损坏数据。第 41 行 `dee = std::min(10000.0, std::stod(v))` 就是上面提到的上限截断。

再看键的构造——这是 `'code\tword'` 格式的诞生地：

[src/rime/dict/user_dictionary.cc:431](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L431) `string key(code_str + '\t' + entry.text);` 提交时按此格式生成键。

[src/rime/dict/user_dictionary.cc:539-540](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L539-L540) 查询时 `CreateDictEntry` 用 `key.find('\t')` 反向拆出 `text`（分隔符之后）与 `full_code`（分隔符之前）。两端严格对称。

对文本后端，`TextDb` 还要能把 `'code\tword'` 拆成 TSV 的两列存储，由一对 parser/formatter 完成：

[src/rime/dict/user_db.cc:67-92](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db.cc#L67-L92) `userdb_entry_parser` 把 TSV 行（code、word、value 三列）拼回 `'code\tword'` 键（注意第 73-74 行会补一个结尾空格，修复旧版本产生的非法键）；`userdb_entry_formatter` 反过来把键按 `\t` 拆成两列。注释 `// key ::= code <space> <Tab> phrase` 就是这条格式的规格说明。

接着看 `UserDbWrapper` 如何把通用 `Db` 装扮成用户库：

[src/rime/dict/user_db.h:79-97](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db.h#L79-L97) 三个虚函数覆写：`CreateMetadata` 在基类之后追加 `UserDbHelper(this).UpdateUserInfo()`（写 `/user_id`）；`Backup`/`Restore` 用 `IsUniformFormat` 判断快照是不是统一的 `.userdb.txt`，是则走跨格式统一路径，否则退回 `BaseDb` 原生实现。

[src/rime/dict/user_db.h:100-109](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db.h#L100-L109) `UserDbComponent` 工厂：`Create(name)` 调 `DbFilePath(name, extension())` 解析路径再 `new UserDbImpl(...)`。`extension()` 的两个特化见 [user_db.cc:56-59](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db.cc#L56-L59)（`.userdb.txt`）与 [level_db.cc:328-331](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/level_db.cc#L328-L331)（`.userdb`）。

最后看一个跨设备同步会用到的「合并器」`UserDbMerger`（4.4 综合实践会再提）：

[src/rime/dict/user_db.cc:204-224](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db.cc#L204-L224) `Put` 实现了「三向合并」：对每条键，把我方（`o`）与对方（`v`）两条记录都先用 `formula_d` 衰减到同一个参考 tick，然后取 `commits` 绝对值更大者、`dee` 取较大者，最后把 `tick` 统一为双方的最大值。这保证两台设备各自学习到的词条合并后不丢失、且时间轴对齐。

#### 4.2.4 代码实践

**实践目标**：手工走一遍 `UserDbValue` 的 `Pack`/`Unpack` 往返，理解值字符串的结构。

**操作步骤**：

1. 假设一条词已被提交 3 次、当前全局 `tick = 100`、`dee = 1.5`。按 [user_db.cc:22-26](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db.cc#L22-L26) 的格式，写出它的 value 字符串。
2. 把这条字符串喂回 `Unpack`，按 [user_db.cc:28-52](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db.cc#L28-L52) 的逻辑手工解析，确认能得到 `commits=3, dee=1.5, tick=100`。
3. 构造一条「损坏」的 value，例如 `"c=3 d=not_a_number t=100"`，预测 `Unpack` 的返回值与日志。
4. （可选）在 `test/user_db_test.cc` 的 `AccessRecordByKey` 里临时把第 23 行的 `"ZYX"` 换成一条真实的 `UserDbValue().Pack()` 输出，再 `Fetch` 出来看是否原样往返。

**需要观察的现象**：value 是三个 `key=value` 片段用空格拼接的纯文本，字段顺序固定为 `c`、`d`、`t`；解析时按空格切分、按等号取键值，字段缺失或类型不符会被 `try/catch` 兜住。

**预期结果**：第 1 步得到 `"c=3 d=1.5 t=100"`；第 2 步解析成功；第 3 步 `Unpack` 返回 `false` 并打印一条 `ERROR` 日志（`std::stod` 抛异常被捕获），此时 `commits`/`dee`/`tick` 保持半解析状态。

#### 4.2.5 小练习与答案

**练习 1**：用户库的键为什么把编码（code）放在前面、词条文本（word）放在后面，而不是反过来？

**参考答案**：因为查询（4.3 的 `DfsLookup`）是「按编码前缀扫描」的——给定当前音节前缀（如 `"ni hao "`），要快速定位所有以它开头的词条。键按字典序排列时，相同编码前缀的键天然聚成连续区段，一次 `Jump(prefix)` + 顺序遍历就能取全。如果把 word 放前面，相同编码的词会散落在不同位置，前缀扫描失效。这和静态码表用 `syllable_id` 序列做索引键是同一个思想——「按查询路径排序」。

**练习 2**：`commits` 用负值表示「软删除」而不是直接 `Erase` 掉，有什么好处？

**参考答案**：两个好处。一是**可恢复**：用户误删后，再次提交该词时 `UpdateEntry` 会 `v.commits = -v.commits`「复活」它（[user_dictionary.cc:443-444](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L443-L444)），不丢学习历史。二是**跨设备合并稳定**：如果直接物理删除，两台设备同步时无法区分「这台删了」与「那台从没学过」，合并会错误地复活；软删除是一条显式的「删除记录」，合并时 `UserDbMerger` 能取 `commits` 绝对值更大者（即「更晚的状态」）正确传播删除意图（[user_db.cc:219-220](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db.cc#L219-L220)）。

---

### 4.3 UserDictionary：DFS 查询与提交时的权重更新

#### 4.3.1 概念说明

`UserDictionary` 是用户词典的**门面与大脑**，对应组件名 `user_dictionary`。它持有：

- 一个 `Db`（4.1/4.2 的用户库），所有读写最终落在这里；
- 一个 `Table` 与一个 `Prism`（来自静态词典，通过 `Attach` 注入），用来在 `syllable_id` 与拼写串之间互译（因为库里存的键是拼写串，而音节图里跑的是 `syllable_id`）；
- 一个全局 `tick_` 计数器（从库的 `/tick` 元数据读出）。

它对外暴露两组核心能力，恰好对应「读」与「写」：

| 能力 | 方法 | 何时调用 |
|------|------|----------|
| 读（查候选） | `Lookup(SyllableGraph, ...)` | 翻译器翻译时，与 `Dictionary::Lookup` 并列 |
| 读（按字符串查） | `LookupWords(...)` | 反查/形码场景 |
| **写（学习）** | `UpdateEntry(entry, commits)` | **用户提交一个词上屏时** |
| 事务 | `NewTransaction` / `RevertRecentTransaction` / `CommitPendingTransaction` | 提交会话开始 / 退格撤销 / 会话结束 |

本模块重点讲两件事：**查询**（`Lookup` 的 DFS）与**权重更新**（`UpdateEntry` 如何改 `commits`/`tick`/`dee`）。后者正是「用户词典会学习」的核心。

#### 4.3.2 核心流程

**查询路径 `Lookup`（DFS）**：

与 `Dictionary::Lookup`（u8-l5）用队列做 BFS 不同，`UserDictionary::Lookup` 用**递归 DFS** 沿音节图走。原因是用户库是按键排序的键值库，最适合「前缀扫描 + 顺序读」的访问模式：DFS 维护一条「当前编码前缀」字符串，在库的游标上 `Jump(prefix)` 定位、顺序读取所有以该前缀开头的键，每读到一条就转成候选；然后沿音节图的 `indices` 表向下递归（前缀追加下一个音节），天然契合键的字典序。

```
DfsLookup(syll_graph, current_pos, current_prefix, state):
  对 current_pos 出发的每条边 (syllable_id, 属性列表):
    把 syllable_id 翻译成拼写，追加进 prefix        # TranslateCodeToString
    state.ForwardScan(prefix)                       # 游标 Jump(prefix) + 读一条
    while 当前键 == prefix + '\t...' (精确匹配):     # IsExactMatch
      state.RecruitEntry(end_pos)                   # 构造 DictEntry，塞进 query_result[end_pos]
      读下一条
    若 end_pos 还有后续边:
      若深度未超限且当前键是 prefix 的前缀:          # IsPrefixMatch
        DfsLookup(syll_graph, end_pos, prefix, state)   # 递归下钻
```

结果和 `Dictionary::Lookup` 一样，是 `UserDictEntryCollector = map<size_t, UserDictEntryIterator>`（按 `end_pos` 分桶），复用了 u8-l5 讲过的 collector 模型。

**权重更新路径 `UpdateEntry`（提交时）**——这是本讲的重头戏：

```
UpdateEntry(entry, commits):            # commits: +1=提交, 0=遇到未选, -1=删除
  key = entry.custom_code + '\t' + entry.text
  若库里有旧值: Unpack 出 v；若 v.tick 异常偏大则修正为 tick_
  根据 commits 三分支:
    commits > 0 (提交):
        若 v.commits < 0: 复活 (取绝对值)
        v.commits += commits
        UpdateTickCount(1)              # 全局 tick_ += 1，并写回 /tick
        v.dee = formula_d(commits, tick_, v.dee, v.tick)   # 带衰减累加
    commits == 0 (翻译时遇到但未选):
        v.dee = formula_d(0.1, tick_, v.dee, v.tick)       # 小幅衰减累加
    commits < 0 (删除):
        v.commits = min(-1, -v.commits)  # 标记软删除
        v.dee = formula_d(0.0, tick_, v.dee, v.tick)       # 衰减归零方向
  v.tick = tick_                        # 记录「最后一次更新时的 tick」
  库.Update(key, v.Pack())              # 写回
```

关键数学是 `formula_d`：

\[
\text{dee}_{\text{新}} = d + \text{dee}_{\text{旧}} \cdot \exp\!\left(\frac{t_{\text{旧}} - t_{\text{现在}}}{200}\right)
\]

其中 \(d\) 是本次贡献（提交为 `commits`=1，遇到为 0.1，删除为 0），\(t\) 是 tick。指数项 \(\exp((t_{\text{旧}}-t_{\text{现在}})/200)\)：若旧记录离现在很久（\(t_{\text{旧}} \ll t_{\text{现在}}\)），该值远小于 1，**旧的 `dee` 被大幅衰减**后再累加新的贡献；200 是衰减常数（约每 200 次提交衰减到 \(1/e\)）。于是 `dee` 成为一个「记得近期、淡忘远期」的指数移动平均——你最近常打的词 `dee` 高，很久没打的词 `dee` 慢慢回落。

查询时，每条候选的权重由 `formula_p` 把「提交频率」（`commits/tick`）与「效能」（`dee`）融合，再取对数：

\[
\text{weight} = \log\bigl(\text{formula\_p}(0,\ \text{commits}/\text{tick},\ \text{tick},\ \text{dee})\bigr) + \text{credibility}
\]

#### 4.3.3 源码精读

先看 `UserDictionary` 的数据成员与装配：

[src/rime/dict/user_dictionary.h:49-107](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.h#L49-L107) 注意它继承 `Class<UserDictionary, const Ticket&>`（是个组件），私有成员 `db_`/`table_`/`prism_`/`syllabary_`/`rev_syllabary_`/`tick_`/`transaction_time_`。`Attach(table, prism)`（第 54 行）由 `Memory` 在构造时调用，注入静态词典的表与棱镜。

[src/rime/dict/user_dictionary.cc:573-586](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L573-L586) `UserDictionaryComponent::Create(dict_name, db_class)`：先 `db_pool_[dict_name].lock()` 尝试从 `weak_ptr` 缓存复用 Db（与 `DictionaryComponent` 缓存 Prism/Table 同理，同名用户库在进程内只开一次）；未命中则 `Db::Require(db_class)` 查工厂、`Create`、登记进 `db_pool_`。

[src/rime/dict/user_dictionary.cc:588-613](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L588-L613) `Create(Ticket)` 读方案配置：`enable_user_dict`（默认 true，关掉则返回 NULL，不创建用户词典）、`user_dict`（用户库名，未写则由 `dictionary` 名推导，如 `luna_pinyin.extra` → `luna_pinyin`）、`db_class`（默认 `"userdb"`，即 LevelDb 后端）。

接着看查询主路 `Lookup` 与 `DfsLookup`：

[src/rime/dict/user_dictionary.cc:314-356](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L314-L356) `Lookup`：第 330 行 `db_->Query("")` 拿全量游标、第 331 行 `Jump(" ")` 跳过元数据；第 327 行 `state.present_tick = tick_ + 1`（用「下一次提交」的 tick 作基准，保证刚查到的候选权重衰减计算一致）；然后调 `DfsLookup`；最后第 337-354 行对每个 `end_pos` 分组 `Sort()`，并把预测查询里的精确匹配候选轮转到队首。

[src/rime/dict/user_dictionary.cc:216-303](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L216-L303) `DfsLookup`：第 220 行从 `syll_graph.indices` 取当前起点；第 230 行把音节压入 `state.code`（用 `BOOST_SCOPE_EXIT` 保证递归返回时弹出，第 231-234 行）；第 235 行 `TranslateCodeToString` 把 `syllable_id` 序列翻成拼写前缀；第 239-240 行 `if (i > 0 && props->type >= kAbbreviation) continue;` 只在每个音节的**第一条非缩写拼写**上扫描（避免缩写引发回溯，见文件顶部 2013-06-25 注释）；第 256-266 行精确匹配循环 `RecruitEntry`；第 291-296 行递归下钻。

[src/rime/dict/user_dictionary.cc:73-101](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L73-L101) `RecruitEntry`：调静态方法 `CreateDictEntry` 把键值对变成 `DictEntry`，塞进 `query_result[pos]`。

[src/rime/dict/user_dictionary.cc:532-567](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L532-L567) `CreateDictEntry`：第 539 行拆键得 `text`、第 543-546 行 `Unpack` 并跳过 `commits < 0` 的已删词条；第 547-548 行若记录的 `tick` 早于现在，先用 `formula_d(0, present_tick, v.dee, v.tick)` 把 `dee` 衰减到当前时刻；第 554-556 行套用 `formula_p` 算权重并取对数加 `credibility`——这就是本模块的权重公式。

现在看本模块最核心的 `UpdateEntry`：

[src/rime/dict/user_dictionary.cc:425-457](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L425-L457) 完整对应 4.3.2 流程图。逐行对照：

- 第 428-430 行：取编码串（优先用 `entry.custom_code`，否则用 `TranslateCodeToString` 把 `entry.code` 即 `syllable_id` 序列翻成拼写——这就是 4.2 里「键是拼写串」与「音节图是 id」之间需要 `Table`/`Prism` 桥接的原因）。
- 第 431 行：构造键 `code_str + '\t' + entry.text`。
- 第 434-441 行：`Fetch` 旧值并 `Unpack`；第 436-438 行修正异常偏大的 `tick`（防御坏数据）。
- 第 442-446 行（`commits > 0`）：复活已删词（`-v.commits`）、累加 `commits`、`UpdateTickCount(1)`、用 `formula_d(commits, ...)` 重算 `dee`。
- 第 448-450 行（`commits == 0`）：用很小的贡献 `k = 0.1` 衰减累加 `dee`（翻译时遇到但未被选中，相当于「轻微温习」）。
- 第 451-454 行（`commits < 0`）：软删除（`commits` 置为 ≤ -1）、`dee` 向 0 衰减。
- 第 455-456 行：统一把 `v.tick` 设为当前 `tick_`，写回库。

[src/rime/dict/user_dictionary.cc:459-466](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L459-L466) `UpdateTickCount`：`tick_ += increment`，并把新值写进元数据键 `/tick`。**这是「提交一次，全局逻辑时钟 +1」的实现**——`tick_` 持久化在库里，重启后由 `FetchTickCount` 读回。

[src/rime/dict/user_dictionary.cc:472-484](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L472-L484) `FetchTickCount`：从 `/tick`（兼容旧版的空键 `""`）读出 `tick_`，用 `std::stoul` 解析，`try/catch` 兜底。

最后看两个衰减公式本身：

[src/rime/algo/dynamics.h:6-8](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/dynamics.h#L6-L8) `formula_d`：带衰减的累加，常数 200 控制衰减快慢。

[src/rime/algo/dynamics.h:10-15](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/dynamics.h#L10-L15) `formula_p`：`m` 是「已学习到的频率」（随 `tick` 增长趋向真实频率 `u = commits/tick`），再按 `dee` 分段映射——`dee` 小时线性趋近 0.5，`dee` 大时指数趋近 1，把「频率」与「近期效能」融合成单个优先级。

#### 4.3.4 代码实践

**实践目标**：手工模拟「一个新词被提交 3 次」的全过程，追踪 `commits`/`tick`/`dee` 的变化，验证 `UpdateEntry` 与 `formula_d` 的行为。

**操作步骤**：

假设用户库为空、全局 `tick_ = 0`，用户首次上屏了词 `"你好"`（编码 `"ni hao "`）。设初始 `dee = 0`。

1. **第 1 次提交**（`UpdateEntry(entry, 1)`）：键里没有旧值，`v` 全为 0/默认。走 `commits > 0` 分支：`v.commits = 0 + 1 = 1`；`UpdateTickCount(1)` 使 `tick_` 从 0 变 1；`v.dee = formula_d(1, 1, 0, 0) = 1 + 0*exp(...) = 1`；`v.tick = 1`。写回 `"c=1 d=1 t=1"`。
2. **第 2 次提交**（紧接着，`tick_` 仍为 1，提交后变 2）：`Fetch` 出旧 `v{c=1,d=1,t=1}`。`v.commits = 1+1 = 2`；`tick_` 变 2；`v.dee = formula_d(1, 2, 1, 1) = 1 + 1*exp((1-2)/200) = 1 + exp(-0.005) ≈ 1 + 0.995 = 1.995`；`v.tick = 2`。写回 `"c=2 d=1.995 t=2"`。
3. **很久以后第 3 次提交**（假设期间全局已累计到 `tick_ = 500`，本词一直没被用过）：`Fetch` 出 `v{c=2,d=1.995,t=2}`。`v.commits = 3`；`tick_` 变 501；`v.dee = formula_d(1, 501, 1.995, 2) = 1 + 1.995*exp((2-501)/200) = 1 + 1.995*exp(-2.495) ≈ 1 + 1.995*0.0826 ≈ 1.165`；`v.tick = 501`。注意：久未使用让旧 `dee` 从 1.995 衰减到约 0.165 后再加 1。
4. 对照 [user_dictionary.cc:425-457](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L425-L457) 与 [dynamics.h:6-8](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/dynamics.h#L6-L8)，确认每一步的字段值。

**需要观察的现象**：`commits` 每次提交严格 +1；`tick_` 每次提交严格 +1（且写进 `/tick` 元数据）；`dee` 不是简单累加，而是先把旧值按「距上次更新的 tick 差」做指数衰减，再累加本次贡献——所以「连续提交」时 `dee` 接近线性增长，「久未使用后再提交」时旧贡献几乎被衰减殆尽。

**预期结果**：第 3 步的 `dee ≈ 1.165` 远小于「不衰减时的 2.995」，体现了「淡忘远期、记得近期」的指数移动平均特性。这解释了为什么你长期不打的用户词会慢慢从候选前排退下。

> 说明：以上数值为按公式手工演算的示例，旨在讲清机制；若要代码验证，可在 `test/user_db_test.cc` 风格的测试里对一个 `TestDb` 反复 `UpdateEntry` 后 `Fetch` 出来核对 `Pack()` 字符串。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`UserDictionary::Lookup` 用 DFS，而 `Dictionary::Lookup`（u8-l5）用 BFS。为什么用户词典选 DFS？

**参考答案**：因为用户库的底层是按键排序的键值存储（LevelDB/TextDb），其游标只支持「`Jump(prefix)` 定位 + 顺序向前读」，不支持随机回退。DFS 维护一条「当前编码前缀」字符串，递归下钻时前缀只增不减，恰好契合游标顺序向前的访问模式（`ForwardScan` 后用 `IsPrefixMatch` 决定能否继续下钻）。BFS 要在多个分叉间来回切换状态、甚至回退游标（backdate），键值库做起来代价高。文件顶部注释（[user_dictionary.cc:193-214](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L193-L214)）专门讨论了为避免回溯而不得不放弃某些缩写路径的取舍。

**练习 2**：`UpdateEntry` 的 `commits == 0` 分支有什么用？谁会以 0 调用它？

**参考答案**：`commits == 0` 表示「这个词在翻译过程中出现过、但用户最终没有选中它上屏」。它用一个很小的贡献 `k = 0.1` 调 `formula_d`，相当于给 `dee` 做一次「轻微温习」——既不增加 `commits`（没真正提交），也不推进全局 `tick`，但让这条词的近期效能略有回升。调用方见 [script_translator.cc:338](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.cc#L338) 的 `user_dict_->UpdateEntry(*e, 0)`。这让「经常出现在候选里但偶尔才被选」的词不会衰减得太快。

**练习 3**：为什么 `tick_` 要持久化在 `/tick` 元数据里，而不是每次启动从 0 开始？

**参考答案**：因为 `formula_d` 的衰减依赖「记录的 `tick` 与当前 `tick` 的差」。如果重启后 `tick_` 归零，所有历史记录的 `v.tick` 都会大于当前 `tick_`（「来自未来」），衰减公式会给出错误的放大而非衰减。把 `tick_` 持久化，保证逻辑时钟单调递增、跨重启连续，衰减才有意义。`FetchTickCount`（[user_dictionary.cc:472-484](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L472-L484)）就是启动时把它读回来的环节。

---

### 4.4 提交闭环、事务、同步与崩溃恢复

#### 4.4.1 概念说明

前面三个模块讲的是「数据结构与单次操作」。本模块把它们串成**端到端的闭环**，回答三个实际场景：

1. **提交闭环**：用户敲完一段输入、按回车上屏，引擎怎么把这次提交翻译成「用户词典里 `commits+1`」？答案在一个叫 `Memory` 的中间层。
2. **撤销与事务**：上屏后 3 秒内按退格，能把刚记下的学习撤回——这是 4.1 事务能力的真实用法。
3. **同步与恢复**：`sync_user_data` 同步到云端/另一台设备时发生了什么？数据库文件损坏打不开时怎么办？

`Memory`（[src/rime/gear/memory.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/memory.h)）是连接「引擎提交信号」与「用户词典写操作」的桥梁。它由 `ScriptTranslator` / `TableTranslator` 持有（u6-l4），构造时同时创建 `Dictionary`（静态）与 `UserDictionary`（动态），并把两者 `Attach` 到一起；同时订阅 `Context` 的 `commit_notifier_`——**每段输入上屏，`Memory::OnCommit` 就被触发**。

#### 4.4.2 核心流程

**提交闭环**：

```
用户上屏一段输入
  │  Context::commit_notifier_ 触发
  ▼
Memory::OnCommit(ctx)
  ├─ user_dict_->NewTransaction()        # 开事务（攒 WriteBatch）
  ├─ 遍历 ctx->composition() 的每段：
  │    ProcessSegmentOnCommit:
  │      取出选中候选 Phrase
  │      commit_entry.AppendPhrase(phrase)   # 累积 text + code
  │      若段已确认：commit_entry.Save()
  │           └─ memory->Memorize(commit_entry)
  │                └─（ScriptTranslator 覆写）
  │                   user_dict_->UpdateEntry(commit_entry, 1)   # commits+1, tick+1, 重算 dee
  ▼
（随后）用户继续敲键 → OnUnhandledKey
  └─ 若是普通键：user_dict_->CommitPendingTransaction()  # 提交事务（落盘）
     若是 BackSpace 且在 3 秒内：DiscardSession()
           └─ user_dict_->RevertRecentTransaction()       # 中止事务（丢弃刚记的学习）
```

**同步（`sync_user_data`）**：C API `sync_user_data` 触发部署器跑三个任务：`installation_update`、`backup_config_files`、`user_dict_sync`。其中 `user_dict_sync`（`UserDictSync::Run`）对每个用户库调 `UserDictManager::Backup`，把库以统一的 `.userdb.txt` 文本快照写到同步目录——这种文本格式跨 LevelDb/TextDb 通用，便于在另一台设备用 `UserDbMerger` 三向合并（4.2.3 讲过）后导入。

**崩溃恢复**：用户库打开失败（文件损坏）时，`UserDictionary::Load` 会向部署器调度一个 `userdb_recovery_task`，在后台线程尝试 `leveldb::RepairDB`；修复失败则把坏文件改名为 `.old`、重建空库，并从同步目录的快照 `Restore` 回来。

#### 4.4.3 源码精读

先看 `Memory` 如何把提交翻译成记忆：

[src/rime/gear/memory.h:36-66](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/memory.h#L36-L66) `Memory` 持有 `dict_` 与 `user_dict_`，声明纯虚 `Memorize`（由具体翻译器实现），并订阅三个 `Context` 信号。

[src/rime/gear/memory.cc:58-91](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/memory.cc#L58-L91) 构造函数：`Require("dictionary")` 与 `Require("user_dictionary")` 创建两本词典、`Load`、然后第 73 行 `user_dict_->Attach(dict_->primary_table(), dict_->prism())` 把静态词典的表与棱镜注入用户词典（供 `syllable_id` ↔ 拼写互译）。第 85-90 行连接三个信号。

[src/rime/gear/memory.cc:99-109](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/memory.cc#L99-L109) 会话三件套：`StartSession` = `NewTransaction`，`FinishSession` = `CommitPendingTransaction`，`DiscardSession` = `RevertRecentTransaction`。

[src/rime/gear/memory.cc:128-136](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/memory.cc#L128-L136) `OnCommit`：先 `StartSession`（开事务），遍历 `composition` 的每段调 `ProcessSegmentOnCommit`。

[src/rime/gear/memory.cc:111-126](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/memory.cc#L111-L126) `ProcessSegmentOnCommit`：取出选中候选、`AppendPhrase` 累积；段状态 `>= kConfirmed` 时 `Save()`——最终触发 `Memorize`。

[src/rime/gear/script_translator.cc:344-346](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.cc#L344-L346) `ScriptTranslator::Memorize` 的全部实现就一行：`user_dict_->UpdateEntry(commit_entry, 1);`——**这就是「提交一次 → `commits+1`」的最终落点**。

[src/rime/gear/memory.cc:151-160](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/memory.cc#L151-L160) `OnUnhandledKey`：普通键 → `FinishSession`（提交事务落盘）；`BackSpace` → `DiscardSession`（撤销）。结合下面的 `RevertRecentTransaction`：

[src/rime/dict/user_dictionary.cc:486-510](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L486-L510) `NewTransaction` 先把上一个未提交的事务 `CommitPendingTransaction` 落盘再开新的；`RevertRecentTransaction` 第 499 行**只在 `time(NULL) - transaction_time_ <= 3` 秒内**才允许 `AbortTransaction`——这就是「3 秒内退格撤销上次提交」的完整实现，事务底层是 4.1 讲的 `WriteBatch`。

再看同步：

[src/rime_api_impl.h:138-145](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L138-L145) `RimeSyncUserData`：先 `CleanupAllSessions`（避免边写边同步），再调度三个任务并 `StartMaintenance`（后台线程跑）。

[src/rime/lever/user_dict_manager.cc:51-71](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/user_dict_manager.cc#L51-L71) `Backup(dict_name)`：以只读打开库（第 53 行）、校验 `user_id`（第 55-61 行，不匹配则重建元数据）、在同步目录写出 `<dict_name>.userdb.txt` 快照（第 69-70 行调 `db->Backup`，LevelDb 后端最终走 `UniformBackup`，把二进制库导出为通用文本格式）。

最后看崩溃恢复任务：

[src/rime/dict/user_dictionary.cc:169-183](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L169-L183) `Load` 在 `db_->Open()` 失败时（第 172 行），若 db 实现了 `Recoverable` 且部署器空闲，就 `ScheduleTask` 一个 `userdb_recovery_task` 并 `StartWork`——**损坏检测是惰性的，修复在后台线程**，不阻塞输入。

[src/rime/dict/user_db_recovery_task.cc:17-58](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db_recovery_task.cc#L17-L58) `Run`：构造时（第 19 行）先 `db_->disable()` 防止其它线程再访问；`BOOST_SCOPE_EXIT`（第 27-30 行）保证无论成功失败都 `enable()`；第 36-39 行先试 `Recover()`（`leveldb::RepairDB`）；修复失败则第 42-49 行把坏文件改名 `.old`（或 `Remove`），第 51 行重新 `Open` 建空库，第 55 行 `RestoreUserDataFromSnapshot` 从同步目录的快照恢复。

[src/rime/dict/user_db_recovery_task.cc:60-83](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db_recovery_task.cc#L60-L83) `RestoreUserDataFromSnapshot`：在同步目录依次尝试 `<dict>.userdb.txt`（统一格式）与遗留的 `<dict>.userdb.snapshot`，找到就 `db_->Restore`。

[src/rime/dict/dict_module.cc:42](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_module.cc#L42) 把恢复任务注册为组件 `userdb_recovery_task`，供 `Load` 用 `DeploymentTask::Require` 取用。

#### 4.4.4 代码实践

**实践目标**：把「提交 → 记忆 → 撤销 / 同步」串成一条可追踪的链，验证事务边界与同步产物。

**操作步骤**：

1. **追踪提交闭环**。从 [memory.cc:128](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/memory.cc#L128) `OnCommit` 出发，依次跳转：`ProcessSegmentOnCommit`（[memory.cc:111](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/memory.cc#L111)）→ `CommitEntry::Save`（[memory.cc:40](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/memory.cc#L40)）→ `Memorize`（[script_translator.cc:344](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.cc#L344)）→ `UpdateEntry(entry, 1)`（[user_dictionary.cc:425](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L425)）。在一张纸上记下：这次调用让 `commits`、`tick_`、`dee` 各发生了什么变化（对照 4.3.4）。
2. **追踪撤销**。阅读 [memory.cc:151-160](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/memory.cc#L151-L160) 与 [user_dictionary.cc:495-502](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L495-L502)。问自己：若上屏后第 4 秒才按退格，会发生什么？（答：`time(NULL) - transaction_time_ > 3` 返回 false，不撤销；此前 `OnCommit` 里攒进 `WriteBatch` 的改动早已在后续某个普通键触发 `FinishSession` 时落盘，无法回滚。）
3. **追踪同步**。阅读 [rime_api_impl.h:138-145](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L138-L145) 与 [user_dict_manager.cc:51-71](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/user_dict_manager.cc#L51-L71)。确认：同步产物是同步目录下的 `<dict_name>.userdb.txt` 文本快照，而非 LevelDB 的二进制文件——这种格式选择是为了跨设备/跨后端通用。
4. **（可选，需运行环境）** 用 u1-l5 的 `rime_api_console`：先 `select schema luna_pinyin`，输入一段拼音并上屏几次，再调用 `sync_user_data`（或退出后看用户数据目录），观察是否生成了 `luna_pinyin.userdb.txt`。**待本地验证**。

**需要观察的现象**：一次提交最终落点在 `UpdateEntry(entry, 1)`，使 `commits+1`、`tick_+1`（写 `/tick`）、`dee` 重算；撤销依赖事务尚未提交（3 秒内 + 尚未敲下一个普通键）；同步产生的是文本快照而非二进制库。

**预期结果**：能复述完整闭环 `commit_notifier → OnCommit → NewTransaction → Memorize → UpdateEntry(1) → 后续按键 FinishSession(CommitTransaction) 落盘`；能解释为何撤销有「3 秒」与「事务未提交」双重前提。

#### 4.4.5 小练习与答案

**练习 1**：`Memory::OnCommit` 先 `NewTransaction`，但事务并不会在 `OnCommit` 结束时提交。它何时才真正落盘？

**参考答案**：在用户**敲下下一个普通按键**时，`OnUnhandledKey` 调 `FinishSession` → `CommitPendingTransaction` → `LevelDb::CommitTransaction`（把 `WriteBatch` 一次性写入，[level_db.cc:319-326](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/level_db.cc#L319-L326)）。这个延迟提交的设计正是为了给「3 秒内退格撤销」留出窗口：在事务提交前，`AbortTransaction` 清空批次即可无成本撤销。若用户在窗口内没按退格、而是继续输入，新提交就顺理成章地落盘固化。

**练习 2**：`UserDbRecoveryTask` 为什么在构造时就 `db_->disable()`，又在 `Run` 结束时 `enable()`？

**参考答案**：因为恢复（`RepairDB`、改名、重建、回填）期间数据库处于不一致状态，绝不能让其它线程（比如正在输入的主线程）继续读写它。构造时 `disable()` 让 `UserDictionary::loaded()`（[user_dictionary.cc:185-187](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L185-L187) 检查 `!db_->disabled()`）返回 false，从而所有查询/更新短路返回；`BOOST_SCOPE_EXIT` 保证无论 `Run` 成功失败都恢复 `enable()`，避免库被永久禁用。

**练习 3**：同步为什么要导出成 `.userdb.txt` 文本快照，而不是直接复制 `.userdb`（LevelDB 二进制目录）？

**参考答案**：两个原因。一是**跨后端通用**：目标设备可能用不同的 `db_class`（比如从 LevelDb 换成 TextDb 或未来的新后端），二进制 LevelDB 文件无法被其它后端读取，而文本快照是「统一格式」（`UserDbHelper::IsUniformFormat` / `UniformBackup` / `UniformRestore` 这套机制的存在意义）。二是**可合并**：文本快照能被 `UserDbMerger`（4.2.3）逐条三向合并，把两台设备各自的学习融合而不丢失；直接覆盖二进制文件只能「整体替换」，会丢掉其中一台的学习历史。

---

## 5. 综合实践

把本讲四个模块串起来，设计一个**「一个词的一生」**追踪任务：从首次被学到、反复提交、久未使用、跨设备同步、直到数据库损坏恢复，画出每个阶段用户库里这条记录与全局 `tick_` 的变化。

**任务**：在一张纸上（或 Markdown 里）填写下表，标注每个阶段涉及的关键源码位置与 `UserDbValue` 三个字段的变化。设初始为全新用户库（`tick_ = 0`），观察词 `"你好"`（编码 `"ni hao "`）。

| 阶段 | 触发 | 关键调用链 | `commits` | `tick_` | `dee`（定性） | 涉及源码 |
|------|------|-----------|-----------|---------|--------------|----------|
| 1. 首次上屏 | 提交 | `OnCommit→Memorize→UpdateEntry(1)` | 0→1 | 0→1 | 0→≈1 | memory.cc:128 / user_dictionary.cc:425 |
| 2. 再次上屏 | 提交 | 同上 | 1→2 | 1→2 | 累加 | user_dictionary.cc:447 |
| 3. 翻译时遇到未选 | 翻译 | `UpdateEntry(0)` | 不变 | 不变 | +0.1（衰减后） | script_translator.cc:338 / user_dictionary.cc:448-450 |
| 4. 3 秒内退格 | BackSpace | `DiscardSession→AbortTransaction` | 回退到阶段前 | 回退 | 回退 | memory.cc:155 / user_dictionary.cc:499 |
| 5. 误删候选 | delete 信号 | `OnDeleteEntry→UpdateEntry(-1)` | → 负值（软删） | +1 | 向 0 衰减 | memory.cc:146 / user_dictionary.cc:451-454 |
| 6. 久未使用后查询 | 翻译 | `CreateDictEntry` 里 `formula_d(0,now,…)` | 不变 | 不变 | 大幅衰减后参与权重 | user_dictionary.cc:547-548 |
| 7. 同步到另一台 | `sync_user_data` | `UserDictSync→Backup→UniformBackup` | 导出文本快照 | — | — | rime_api_impl.h:138 / user_dict_manager.cc:51 |
| 8. 库损坏重启 | `Open` 失败 | `Load→ScheduleTask userdb_recovery_task→Run` | `RepairDB` 或从快照 Restore | — | — | user_dictionary.cc:172 / user_db_recovery_task.cc:23 |

**进阶子任务**：

1. 对阶段 4（撤销），解释为何只有在「3 秒内」且「事务尚未被下一个普通键提交」两个条件同时满足时才生效，并指出这两个条件分别由 [user_dictionary.cc:499](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L499) 的时间判断与 [memory.cc:158](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/memory.cc#L158) 的 `FinishSession` 共同决定。
2. 对阶段 7（同步），写出 `.userdb.txt` 快照里这条记录的两列（code、word）与第三列（value）分别是什么，对照 [user_db.cc:67-92](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_db.cc#L67-L92) 的 formatter。
3. 思考：如果方案里设了 `enable_user_dict: false`（[user_dictionary.cc:592-595](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/user_dictionary.cc#L592-L595)），上表哪些阶段会消失？（答：1/2/3/4/5 全部消失——不创建用户词典，`Memory::user_dict_` 为空，所有 `OnCommit`/`OnDeleteEntry` 在入口处 `if (!user_dict_) return;` 短路；6/7/8 也就无从谈起。）

> 本综合实践为「源码阅读型实践」，不依赖运行环境；若要运行验证，可基于 `rime_api_console`（u1-l5）反复上屏后检查用户数据目录里的 `*.userdb` 与同步后的 `*.userdb.txt`。**待本地验证**运行环境。

## 6. 本讲小结

- 用户词典用一张**键值库**（`Db`）存学习数据：键是 `'code\tword'`（编码串 + `\t` + 词条），值是 `UserDbValue{commits, dee, tick}` 打包成的 `"c=.. d=.. t=.."`。`commits` 记提交次数（负值=软删除）、`dee` 是带衰减的近期效能、`tick` 是该记录最后一次更新时的全局逻辑时钟。
- `Db` 是抽象基类（`Fetch`/`Update`/`Query` + 元数据），通过 `Transactional`/`Recoverable` 两个混入声明可选能力；默认后端 `LevelDb`（注册名 `userdb`）用 LevelDB + `WriteBatch` 实现事务、用 `RepairDB` 实现恢复，`TextDb`（`plain_userdb`）是可读的文本后端。元数据键靠 `\x01` 前缀天然排在数据键之前，`Jump(" ")` 即可跳过。
- `UserDictionary` 是门面组件，持有 `Db` + 注入的 `Table`/`Prism`。**查询**用 DFS 沿音节图 `indices` 表前缀扫描用户库（区别于静态词典的 BFS），产出 `map<size_t, UserDictEntryIterator>`（与 u8-l5 同模型）；**更新** `UpdateEntry(entry, commits)` 按 `commits` 正/零/负三分支处理提交/温习/删除，每次提交让全局 `tick_+1` 并用 `formula_d` 重算 `dee`。
- 权重工作在对数空间：`formula_d`（`d + da·exp((ta-t)/200)`）实现「淡忘远期、记得近期」的指数移动平均；查询时 `CreateDictEntry` 用 `formula_p` 融合频率与效能、取对数加 `credibility`。
- 提交闭环由 `Memory` 桥接：`Context::commit_notifier_` → `OnCommit` → `NewTransaction`（攒批次）→ `Memorize` → `UpdateEntry(1)`；下一个普通键触发 `CommitTransaction` 落盘，3 秒内退格则 `AbortTransaction` 撤销。
- `sync_user_data` 把每个用户库导出为统一的 `.userdb.txt` 文本快照（跨后端、可被 `UserDbMerger` 三向合并）；库损坏时 `Load` 惰性调度 `userdb_recovery_task`，先后尝试 `RepairDB`、重建空库、从快照 `Restore`。

## 7. 下一步学习建议

本讲讲完了 librime 词典系统的「动态」一面，至此 u8（词典系统）全单元结束。接下来推荐：

- **u9-1 部署器与部署任务**：本讲多次提到的「部署器（Deployer）调度后台任务」（`userdb_recovery_task`、`user_dict_sync` 都是其 `DeploymentTask`）将在 u9-1 系统讲解——任务队列、工作线程、维护模式。
- **u9-2 部署任务族**：`UserDictSync`、`user_dict_upgrade` 等任务的注册与执行顺序，以及它们如何与 `UserDictManager` 协作完成同步与升级。
- **u9-3 Customizer 与用户设置**：`*.custom.yaml` 是「配置层」的用户定制，而本讲的 `*.userdb` 是「数据层」的用户学习——两者共同构成 RIME 的「个性化」能力，对照阅读能看清分层。
- **回看 u6-l4 Translator 组件族**：带着本讲的知识重读 `script_translator.cc`，重点看它如何同时调用 `Dictionary::Lookup`（静态，u8-l5）与 `UserDictionary::Lookup`（动态，本讲）并把两路候选合并排序——那是静态与动态两套机制的汇合点。
- **延伸阅读**：若想深入「频率衰减」的数学，可对照 [src/rime/algo/dynamics.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/dynamics.h) 手工画 `formula_d` 随 `t-ta` 的衰减曲线，与 `formula_p` 在不同 `dee` 下的分段映射，理解「为什么常打的词会排到前面、久不打的会沉下去」。
