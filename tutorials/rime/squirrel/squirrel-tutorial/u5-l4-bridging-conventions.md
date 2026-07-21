# Swift/C 桥接约定

## 1. 本讲目标

Squirrel 是用 Swift 写的 macOS 输入法前端，但它真正干活的「大脑」librime 是一个 C++ 库，只通过一套 **C 语言 API** 对外暴露。Swift 与 C 之间隔着内存模型、字符串表示、结构体布局的差异。本讲把散落在项目各处的「桥接约定」集中讲清楚。读完本讲，你应当能够：

1. 解释为什么所有 librime 的 C 结构（`RimeTraits` / `RimeCommit` / `RimeStatus` 等）必须用 `.rimeStructInit()` 初始化，而不能用 Swift 默认的 `init()`——核心在于 `data_size` 字段。
2. 说清 `setCString` 用 `strdup` 在 C 堆上分配的字符串「归谁所有、什么时候释放」，并能把它和 `get_*` / `free_*` 的反向所有权做对比。
3. 读懂项目自定义的 `?=`（可选赋值）运算符、`NSRange.empty` 哨兵、`RimeStringSlice.asString` 按长度裁剪的字符串桥接，以及 `NSPoint` 几何工具的设计动机。

本讲是整个手册的「底层粘合剂」：前面 u2-l2（全局 librime 初始化）、u2-l3（控制器生命周期）、u2-l6（rimeUpdate 数据流）、u3-l3（主题加载）都多次点到这些工具，本讲把它们一次性讲透。

## 2. 前置知识

- **C 结构体与 Swift 导入结构体的区别**。Swift 编译器会把 Clang 模块里的 C `struct` 导入成 Swift 的 `struct`，但 C 结构体在内存里是「一块连续的字节」，里面可能包含指针、可能包含编译器为对齐而插入的 **padding（填充字节）**。Swift 默认的成员初始化器只保证「每个存储属性被赋了声明的默认值」，并不会把 padding 和嵌套指针字段也清零。
- **`data_size`（尺寸书签）**。librime 的结构体都遵守一个老式 C API 的版本协商约定：每个结构体的**第一个字段**都是 `data_size`（一个 `Int32`），记录「调用方这个结构体到底有多大」。引擎收到结构体指针时，先读 `data_size`，据此判断能安全地读写多少字节——这样「新版引擎 + 旧结构体」「旧版引擎 + 新结构体」才不会越界。这与 Windows API 里结构体开头的 `cbSize`、许多 C ABI 里的 `sizeof` 字段是同一类技巧。
- **C 字符串 vs Swift `String`**。C 字符串是 `char*`，以 `\0` 结尾的一串字节；Swift 的 `String` 是值类型、内部用 UTF-8 编码但布局对调用方不可见。两者不能直接互相赋值，必须经过桥接。
- **`strdup` / `free`**。`strdup` 是 C 标准库函数，用 `malloc` 分配一块新内存、把字符串拷进去、返回指针；这块内存归调用方所有，必须由调用方 `free`，否则就是内存泄漏。
- 前置讲义：本讲承接 **u2-l2（全局 librime 初始化）**，那里首次用到 `rimeStructInit` 与 `setCString`；也建议回顾 **u2-l6（rimeUpdate 数据流）** 中「配对释放」一节，本讲会与之呼应。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [sources/BridgingFunctions.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift) | 本讲主角。集中定义了 `DataSizeable` 协议、`rimeStructInit()`、`setCString`、`?=` 运算符、`NSRange.empty`、`NSPoint` 几何运算符等所有项目级桥接工具。 |
| [sources/Squirrel-Bridging-Header.h](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Squirrel-Bridging-Header.h) | 桥接头文件，把 librime 的 C 头文件 `rime_api_stdbool.h` 与 `rime/key_table.h` 暴露给 Swift，于是 Swift 才能认识 `RimeTraits` 等结构。 |
| [sources/SquirrelApplicationDelegate.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift) | `setupRime` 用 `rimeStructInit` + `setCString` 填充 `RimeTraits`；文件末尾还定义了 `RimeStringSlice.asString` 这个字符串桥接扩展。 |
| [sources/SquirrelInputController.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift) | 消费侧样板：`rimeConsumeCommittedText` 用 `rimeStructInit` + `get_commit` + `free_commit` 三件套；`?=` 刷新弱引用 `client`；`selRange` 用 `NSRange.empty` 作默认值。 |
| [sources/Main.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift) | `--build` 命令行分支里用 `RimeTraits.rimeStructInit()` + `setCString` 起一个「部署器」身份。 |
| [sources/SquirrelView.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift) | 用到 `NSRange.empty` 作哨兵（`guard range != .empty`）与 `NSPoint` 几何运算符（向量归一化 `number / number.length`）。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① `rimeStructInit` 的零内存与 `data_size`；② `setCString` 的 `strdup`/`free` 所有权；③ `?=` 可选赋值运算符；④ `NSRange.empty` 哨兵、`RimeStringSlice.asString` 与 `NSPoint` 几何工具。

### 4.1 rimeStructInit：C 结构的零内存与 data_size

#### 4.1.1 概念说明

Swift 代码要调用 librime 的 C 函数，比如 `rimeAPI.get_commit(session, &commitText)`。这个函数期望你传进去一个**已经初始化好**的 `RimeCommit` 结构体指针，引擎往里面写定稿文本。问题来了：怎么才算「初始化好」？

C ABI 对结构体的要求比 Swift 严格：

1. **整块内存必须是干净的零**。C 结构体里常常有指针字段（如 `RimeCommit.text: UnsafePointer<CChar>?`）。如果这块字节里残留着上一次使用留下的垃圾值，引擎就会把那个垃圾值当成合法指针去解引用，轻则读到乱码，重则崩溃。Swift 默认的成员初始化器只保证「存储属性取声明的默认值」，对 padding 字节、嵌套字段并不保证清零。
2. **`data_size` 必须被正确填写**。引擎靠结构体开头的 `data_size` 判断「调用方给的结构体有多大」，从而决定能安全填充多少字段。如果 `data_size` 是 0 或错值，引擎会认为「调用方给的结构体是空的 / 太旧」，于是只填一个公共前缀甚至完全不填，`get_commit` 之类就取不到任何东西。

所以项目需要一个统一的初始化工具，**同时**完成「清零」与「填 `data_size`」两件事——这就是 `rimeStructInit()`。

#### 4.1.2 核心流程

`rimeStructInit()` 是定义在 `DataSizeable` 协议扩展上的静态方法，它的工作分三步：

```text
rimeStructInit()
 ├─ 1. allocate(1)         分配「刚好放得下一个本结构」的内存
 ├─ 2. memset(..., 0, size) 把这块内存整体清零（含 padding、指针字段）
 ├─ 3. move()              把零值「搬」进一个 Swift 变量，再 deallocate
 ├─ 4. 计算 data_size：    总字节数 − data_size 字段自身的偏移
 └─ 5. 回写 data_size，返回这个干净的结构
```

它之所以能「对所有 librime 结构通用」，是因为这些结构都被声明为遵守 `DataSizeable` 协议——协议只要求一件事：必须有一个可读写的 `data_size: Int32` 字段。

消费侧的用法是高度同构的样板（见 u2-l6 的三段式）：

```text
var xxx = Xxx.rimeStructInit()        // ① 清零 + 填 data_size
if rimeAPI.get_xxx(session, &xxx) {   // ② 引擎填充（前提：data_size 正确）
    ...使用 xxx...
    _ = rimeAPI.free_xxx(&xxx)        // ③ 配对释放引擎在 C 堆上分配的内容
}
```

#### 4.1.3 源码精读

先看协议与适配列表 [sources/BridgingFunctions.swift:10-19](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L10-L19)：协议 `DataSizeable` 只声明一个 `data_size` 字段；接着用空扩展让五个 librime 结构（`RimeContext_stdbool`、`RimeTraits`、`RimeCommit`、`RimeStatus_stdbool`、`RimeModule`）都遵守它——这是让下面的通用工具能作用于这些 C 结构的「入场券」。

再看核心实现 [sources/BridgingFunctions.swift:22-30](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L22-L30)：

```swift
static func rimeStructInit() -> Self {
  let valuePointer = UnsafeMutablePointer<Self>.allocate(capacity: 1)
  memset(valuePointer, 0, MemoryLayout<Self>.size)
  var value = valuePointer.move()
  valuePointer.deallocate()
  let offset = MemoryLayout.size(ofValue: \Self.data_size)
  value.data_size = Int32(MemoryLayout<Self>.size - offset)
  return value
}
```

逐行解读：

- 第 1 行 `allocate(capacity: 1)`：在堆上申请「刚好放一个 `Self`」的内存。
- 第 2 行 `memset(..., 0, MemoryLayout<Self>.size)`：把这块内存**整体**写成 0。`MemoryLayout<Self>.size` 是结构体占用的字节数。这一步是关键——Swift 默认 `init()` 做不到这件事。
- 第 3 行 `.move()`：把这块零内存「搬」成一个 Swift 的 `Self` 值（所有权从缓冲区转移到变量），第 4 行立即 `deallocate()` 归还缓冲。得到的就是一个「字节全零」的结构体。
- 第 5、6 行：计算并回填 `data_size`。`MemoryLayout<Self>.size` 是结构总体积，减去 `data_size` 字段自身的字节偏移，得到要写回的 `data_size` 值。这遵循 librime 的「按尺寸做 ABI 版本协商」约定：引擎据此判断调用方结构体的版本与可安全访问的范围。变量名 `offset` 也直接点明了这一步的意图——扣除 `data_size` 这个「书签字段」自身占据的位置。

真实使用点在 `setupRime` 中 [sources/SquirrelApplicationDelegate.swift:150](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L150) `var squirrelTraits = RimeTraits.rimeStructInit()`；消费侧三段式在 [sources/SquirrelInputController.swift:427-433](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L427-L433)：

```swift
var commitText = RimeCommit.rimeStructInit()      // ① 清零 + data_size
if rimeAPI.get_commit(session, &commitText) {     // ② 引擎填充
  if let text = commitText.text {
    commit(string: String(cString: text))
  }
  _ = rimeAPI.free_commit(&commitText)            // ③ 配对释放
}
```

#### 4.1.4 代码实践

**实践目标**：亲眼确认「不初始化 / 初始化错」会带来的两类后果，理解 `data_size` 的角色。

1. 打开 [sources/BridgingFunctions.swift:22-30](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L22-L30)，确认 `rimeStructInit` 同时做了 `memset`（清零）与回填 `data_size` 两件事。
2. 打开 [sources/SquirrelInputController.swift:427-433](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L427-L433)，确认 `RimeCommit.rimeStructInit()` 是 `get_commit` 的**前置条件**。
3. **思想实验（不修改源码）**：假设把第 427 行换成 `var commitText = RimeCommit()`（Swift 默认 init），会怎样？
   - `commitText.data_size` 没被填写（保持默认），引擎在 `get_commit` 里读到错误的尺寸 → 可能拒绝填充结构。
   - 结构体里的 `text` 指针字段没被清零 → 即便引擎没写它，代码里 `if let text = commitText.text` 也可能命中垃圾指针，`String(cString: text)` 解引用后崩溃。
4. **可观察现象**：如果你在本地有可编译环境，可以临时把某处 `RimeCommit.rimeStructInit()` 改成 `RimeCommit()`，重新构建运行，输入文字观察：定稿文本无法上屏，或程序直接 crash（具体表现「待本地验证」，因为行为取决于引擎对错误 `data_size` 的容错策略）。
5. **预期结果**：你能口头复述「`rimeStructInit()` = 清零 + 填 `data_size`」这两件事缺一不可，且解释 `data_size` 是 librime 的 ABI 尺寸书签。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `rimeStructInit` 里要用 `memset` 整块清零，而不是依赖 Swift 默认的成员初始化器？

> **参考答案**：Swift 默认初始化器只保证每个存储属性取声明的默认值，对结构体里的对齐 padding 字节、嵌套的 C 指针字段并不保证清零。而 C ABI（尤其 librime）会按字节偏移直接读这些字段——一个没清零的 `char*` 字段会被当成合法指针解引用，导致读到乱码或崩溃。`memset` 保证每一个字节（含 padding 与指针）都是 0，这是 C 结构体跨语言传递的前提。

**练习 2**：`data_size` 字段在 librime 的 ABI 里起什么作用？为什么不能让它保持 0？

> **参考答案**：`data_size` 是 librime 结构体开头的「尺寸书签」，引擎据此判断调用方结构体的版本与可安全读写的字节数，实现「新引擎 ↔ 旧结构体」的前向/后向兼容。如果 `data_size` 是 0，引擎会认为调用方给的是空结构或过旧版本，只填公共前缀甚至完全不填，于是 `get_commit` / `get_status` / `get_context` 都取不到有效数据。所以 `rimeStructInit` 必须把它正确回填。

### 4.2 setCString：Swift→C 字符串的 strdup/free 所有权

#### 4.2.1 概念说明

`RimeTraits` 里有一批字符串字段，如 `shared_data_dir`、`user_data_dir`、`app_name`，它们在 C 里的类型是 `UnsafePointer<CChar>?`（即「指向 C 字符串的指针」）。我们手上只有 Swift 的 `String`，**不能**直接 `squirrelTraits.app_name = "rime.squirrel"`——类型不匹配。

`setCString(_:to:)` 就是这个桥接器：它把 Swift 字符串拷贝成一份 C 字符串，再把指针写进结构体字段。因为它用 `strdup` 在 C 堆上 `malloc` 了一块新内存，所以立刻引出所有权问题：**这块内存归谁？什么时候释放？** 这是本模块的核心，也是本讲实践任务的第二问。

理解所有权，最有效的方式是把 `setCString`（Swift→C）和 `get_*` / `free_*`（C→Swift）这对「镜像方向」放在一起对比。

#### 4.2.2 核心流程

`setCString` 的工作流：

```text
setCString("rime.squirrel", to: \.app_name)
 ├─ 1. withCString { cStr in ... }   临时拿到一个 C 字符串（生命周期仅限闭包内）
 ├─ 2. strdup(cStr)                  在 C 堆上 malloc 一份「长期」拷贝
 ├─ 3. 若该字段已有旧指针 → free 旧指针  （覆盖前先回收，防重复赋值泄漏）
 └─ 4. 把新指针写进 self[keyPath]      （结构体字段持有新指针）
```

两个方向的字符串所有权对比：

| 方向 | 谁分配 | 谁持有 | 谁释放 | 代表 API |
| --- | --- | --- | --- | --- |
| Swift → C（给引擎身份/路径） | 前端（`strdup`） | 结构体字段 | 覆盖同一字段时由 `setCString` 回收；一次性初始化的字符串随引擎 `finalize`/进程退出而结束 | `setCString` |
| C → Swift（取引擎结果） | 引擎（在 C 堆 malloc） | 结构体字段（引擎填） | 前端调用配对的 `free_*` 释放 | `get_commit` + `free_commit` |

记住一句话：**`setCString` 是「我们 `strdup`，我们自己 `free`」；`get_*` / `free_*` 是「引擎 `malloc`，我们替引擎 `free`」。** 两套都不能漏，漏了就泄漏。

#### 4.2.3 源码精读

`setCString` 定义在同一个 `DataSizeable` 扩展里 [sources/BridgingFunctions.swift:32-41](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L32-L41)：

```swift
mutating func setCString(_ swiftString: String, to keypath: WritableKeyPath<Self, UnsafePointer<CChar>?>) {
  swiftString.withCString { cStr in
    // Rime traits keep C string pointers after this closure returns.
    let mutableCStr = strdup(cStr)
    if let existing = self[keyPath: keypath] {
      free(UnsafeMutableRawPointer(mutating: existing))
    }
    self[keyPath: keypath] = UnsafePointer(mutableCStr)
  }
}
```

逐行解读：

- `swiftString.withCString { cStr in ... }`：把 Swift 字符串临时表示成 C 字符串 `cStr`，但**这个 `cStr` 只在闭包返回前有效**（见 Apple 文档对 `withCString` 的约定）。所以不能直接把 `cStr` 存进结构体——出了闭包它就悬空了。注释 `// Rime traits keep C string pointers after this closure returns.` 正是在强调这一点。
- `let mutableCStr = strdup(cStr)`：用 `strdup` 在 C 堆上拷一份**长期有效**的副本，返回 `UnsafeMutablePointer<CChar>`。所有权转移到前端。
- `if let existing = self[keyPath: keypath] { free(...) }`：如果这个字段之前已经有过值（比如重复调用 `setCString` 设同一个字段），先 `free` 掉旧指针，避免泄漏。这一步保证了「对同一字段反复 `setCString` 不会累积泄漏」。
- `self[keyPath: keypath] = UnsafePointer(mutableCStr)`：把新指针写进结构体字段。从此结构体持有这块 C 内存。

真实调用点是 `setupRime` [sources/SquirrelApplicationDelegate.swift:150-158](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L150-L158)：

```swift
var squirrelTraits = RimeTraits.rimeStructInit()
squirrelTraits.setCString(Bundle.main.sharedSupportPath!, to: \.shared_data_dir)
squirrelTraits.setCString(SquirrelApp.userDir.path(), to: \.user_data_dir)
squirrelTraits.setCString(SquirrelApp.logDir.path(), to: \.log_dir)
squirrelTraits.setCString("Squirrel", to: \.distribution_code_name)
squirrelTraits.setCString("鼠鬚管", to: \.distribution_name)
squirrelTraits.setCString(Bundle.main.object(forInfoDictionaryKey: ...) as! String, to: \.distribution_version)
squirrelTraits.setCString("rime.squirrel", to: \.app_name)
rimeAPI.setup(&squirrelTraits)
```

命令行 `--build` 分支里也有同构用法 [sources/Main.swift:72-73](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L72-L73)：`RimeTraits.rimeStructInit()` + `setCString("rime.squirrel-builder", to: \.app_name)`。

镜像方向的对照在 `rimeConsumeCommittedText` [sources/SquirrelInputController.swift:427-433](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L427-L433)：`get_commit` 让引擎在 C 堆上 `malloc` 出 `commitText.text`，所以末尾必须 `rimeAPI.free_commit(&commitText)` 由前端替引擎释放。

#### 4.2.4 代码实践

**实践目标**：把 `setCString` 的所有权讲清楚，并与 `free_commit` 做对比。

1. 打开 [sources/BridgingFunctions.swift:32-41](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L32-L41)，找到 `strdup` 与 `free` 这两处，确认「覆盖前先 `free` 旧值」的回收逻辑。
2. 打开 [sources/SquirrelApplicationDelegate.swift:150-158](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L150-L158)，数一数 `setupRime` 用 `setCString` 设置了几个字段（答案：7 个）。
3. 对比 [sources/SquirrelInputController.swift:432](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L432) 的 `free_commit`，明确两个方向的释放者不同。
4. **思考题（口头追踪）**：如果 `setupRime` 里对 `\.app_name` 连续调用两次 `setCString`，第一次的字符串会不会泄漏？
5. **预期结果**：不会泄漏。第二次 `setCString` 进门时，`if let existing = self[keyPath: keypath]` 命中第一次的指针，先 `free` 再覆盖。这正是那几行 `free` 守卫的存在意义。

#### 4.2.5 小练习与答案

**练习 1**：`setCString` 为什么要先 `strdup` 再赋值，而不是直接把 `withCString` 给的 `cStr` 存进结构体？

> **参考答案**：`withCString` 提供的 `cStr` 是临时缓冲，**只在闭包返回前有效**，出了闭包就被回收/失效。结构体字段要在闭包之外、甚至跨函数长期持有这个字符串（最后交给 `rimeAPI.setup`），所以必须用 `strdup` 在 C 堆上拷一份独立、长期有效的副本，再把这个新指针存进字段。

**练习 2**：`setCString` 用 `strdup` 分配的 C 字符串，由谁、在何时释放？

> **参考答案**：**所有权归结构体字段**。释放发生在两个时机：① 当对**同一字段**再次调用 `setCString` 时，进门先 `free` 掉旧指针（`if let existing ... free(...)`），所以反复赋值不会泄漏；② 对于 `setupRime` 这类一次性初始化，字符串被写进 `RimeTraits`、经 `rimeAPI.setup` 读入引擎后，随结构体一直存活到引擎 `finalize()` / 进程退出（这类一次性、进程生命周期的分配是可接受的）。注意它和 `get_*` / `free_*` 的镜像区别：后者由引擎在 C 堆 `malloc`，必须由前端调用配对的 `free_commit` / `free_status` / `free_context` 显式释放，否则每次按键都泄漏。

### 4.3 ?= 可选赋值运算符

#### 4.3.1 概念说明

Swift 标准库没有「仅当右侧非 `nil` 才赋值」的运算符。但 Squirrel 里这种需求极其频繁：

- 刷新弱引用 `client`：`sender` 可能是 `nil`（这次没拿到有效目标应用），但我们不想把手上还可能有效的旧 `client` 清成 `nil`，只想「有新的就换、没新的就留着」。
- 读配置覆盖默认值：主题里大量属性是非可选类型且已有默认值（如 `var linear: Bool`），从 `config.getString(...)` 读到的是 `Optional`，我们想要「配置里有就用配置的，没有就保持默认」。

为了不在每个调用点都写 `if let` 三行样板，项目自定义了一个 `?=` 运算符。它在前面的 u2-l3、u3-l3 已多次用到，本讲给出它的权威定义。

#### 4.3.2 核心流程

`?=` 的语义只有一句话：**右侧解包成功（非 `nil`）才赋值，右侧为 `nil` 则什么都不做（左侧保持原值）。** 它被定义成两个重载，分别面向「非可选左值」和「可选左值」：

```text
a ?= b          // b: T?
 ├─ 若 b 非 nil：a = b!
 └─ 若 b 为 nil：什么都不做，a 保持原值
```

两个重载的差别只在左值类型：

| 重载 | 左值类型 | 典型场景 |
| --- | --- | --- |
| `func ?=<T>(left: inout T, right: T?)` | 非可选（已有默认值） | 主题加载：`linear ?= config.getString(...).map { ... }` |
| `func ?=<T>(left: inout T?, right: T?)` | 可选 | 刷新弱引用：`self.client ?= sender as? IMKTextInput` |

注意第二个重载的微妙之处：`?=` **永远不会把左值「清成 `nil`**——右值为 `nil` 时它什么也不做。这正好是「保留旧值」的语义，而不是「用 `nil` 覆盖」。

#### 4.3.3 源码精读

运算符的声明与两个重载 [sources/BridgingFunctions.swift:44-56](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L44-L56)：

```swift
infix operator ?= : AssignmentPrecedency
// swiftlint:disable:next operator_whitespace
func ?=<T>(left: inout T, right: T?) {
  if let right = right {
    left = right
  }
}
// swiftlint:disable:next operator_whitespace
func ?=<T>(left: inout T?, right: T?) {
  if let right = right {
    left = right
  }
}
```

解读：

- `infix operator ?= : AssignmentPrecedency`：声明 `?=` 是中缀运算符，优先级与 `=` 相同（`AssignmentPrecedency`），所以它和普通赋值一样从右往左结合、优先级很低。SwiftLint 那两行注释是为了抑制「运算符周围空格」的告警。
- 两个重载的函数体完全一样：`if let right = right { left = right }`。差别只在签名——第一个左值是非可选 `inout T`，第二个左值是可选 `inout T?`。编译器会按左值类型选对应重载。

两种典型用法各看一处：

- **可选左值（刷新弱引用 client）**：[sources/SquirrelInputController.swift:47](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L47) `self.client ?= sender as? IMKTextInput`。`client` 是 `weak var client: IMKTextInput?`（可选），命中第二个重载。`sender as? IMKTextInput` 为 `nil` 时不覆盖，保留旧 client。
- **非可选左值（配置覆盖默认）**：主题加载里几十处，例如 [sources/SquirrelTheme.swift:200](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L200) `inlinePreedit ?= config.getBool("style/inline_preedit")`，命中第一个重载：配置读到 `true`/`false` 就覆盖默认，读不到（`nil`）就保持默认。

#### 4.3.4 代码实践

**实践目标**：通过两个真实调用点，区分两个重载的适用场景。

1. 打开 [sources/BridgingFunctions.swift:44-56](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L44-L56)，确认 `?=` 是项目自定义运算符、优先级是 `AssignmentPrecedency`。
2. 打开 [sources/SquirrelInputController.swift:47](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L47)，把 `self.client ?= sender as? IMKTextInput` 改写成等价的 `if let` 形式：`if let newClient = sender as? IMKTextInput { self.client = newClient }`。体会 `?=` 把三行压成一行的便利。
3. 打开 [sources/SquirrelTheme.swift:200-205](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelTheme.swift#L200-L205)，看连续 6 行 `xxx ?= config.getBool(...)`，理解「配置项不存在就保留默认值」的模式。
4. **需要观察的现象**：`?=` 在右值为 `nil` 时是「什么都不做」，而非「把左值清成 `nil`」。在主题加载里，这意味着没在 YAML 里配的项会保留代码里的默认值（而不是变成 `nil` 导致后面解包崩溃）。
5. **预期结果**：你能说出「左值是非可选 → 命中第一个重载；左值是可选（如 weak client）→ 命中第二个重载」，并解释为什么 `?=` 永远不会把一个可选左值主动清成 `nil`。

#### 4.3.5 小练习与答案

**练习 1**：`self.client ?= sender as? IMKTextInput`，为什么不直接写 `self.client = sender as? IMKTextInput`？

> **参考答案**：直接赋值时，若 `sender as? IMKTextInput` 为 `nil`（这次没拿到有效 client），会把原本可能还有效的旧 `client` 覆盖成 `nil`，导致后续上屏失败。`?=` 只在右侧非 `nil` 时才赋值，保留了「拿不到新的就先用着旧的」的兜底，对随时可能被释放的 `weak` 引用更稳健。

**练习 2**：`?=` 的两个重载函数体完全相同，为什么要分写成两个？

> **参考答案**：因为左值类型不同——一个是非可选 `inout T`（左值已有确定类型的默认值），一个是可选 `inout T?`。Swift 的重载决议按左值类型选择：非可选左值命中第一个，可选左值命中第二个。分开声明是为了让同一运算符同时服务「覆盖非可选默认值」和「刷新可选引用」两种场景，否则编译器无法对可选左值匹配上 `T?` 形参。

### 4.4 NSRange.empty 哨兵、RimeStringSlice.asString 与 NSPoint 几何工具

#### 4.4.1 概念说明

`BridgingFunctions.swift` 末尾还定义了三个小而常用的项目级约定，它们解决的是「空值哨兵」「按长度裁剪的字符串桥接」「二维向量运算」这三类反复出现的小需求。

**① `NSRange.empty` 哨兵。** Cocoa 里 `NSRange` 表示一段区间。面板绘制时经常要表达「当前没有选中区段」「没有预编辑区」——这是「什么都没有」的状态。直接用 `NSRange(location: 0, length: 0)` 不行，因为它表示「在第 0 个字符处有一个长度为 0 的空区间」（一个有意义的光标位置），与「根本没有区间」是两回事。Cocoa 有一个现成的「找不到」哨兵 `NSNotFound`（一个巨大的整数，`Int.max`），项目把它包成 `NSRange(location: NSNotFound, length: 0)`，再起个好记的名字 `NSRange.empty`，全项目统一用它表示「无区间」。

**② `RimeStringSlice.asString`。** librime 有些字符串字段不是裸 `char*`，而是 `RimeStringSlice`（一个「指针 + 长度」的切片）。对于切片，**不能**用 `String(cString:)` 来读——因为 `String(cString:)` 会一直读到 `\0`，而引擎在生成「缩写标签」时会把 `.length` 截到首个字形（当没有显式 `abbrev:` 字段时），切片后面还跟着完整的 `states:` 文本。`String(cString:)` 会越过 `.length` 把整串都读出来，拿到错误的值。`asString` 用 `Data(bytes:count:)` 严格按 `.length` 截取，再按 UTF-8 解码，保证读到的是引擎想给的截断结果。

**③ `NSPoint` 几何运算符。** 候选面板的几何计算（定位、缩放、贝塞尔圆角）里频繁要做二维向量加减、数乘、求模长。AppKit 的 `NSPoint` 没有自带这些运算符，每次都写 `NSPoint(x: a.x + b.x, y: a.y + b.y)` 太啰嗦。项目给 `NSPoint` 扩展了 `+=`、`-`、`-=`、`*`（乘标量）、`/`（除标量）和 `.length`（模长），让向量运算写得像数学公式。

#### 4.4.2 核心流程

**`NSRange.empty` 的判等模式：**

```text
声明默认值：private var selRange: NSRange = .empty
使用前守卫：guard range != .empty else { return nil }
```

整个面板/视图里，「无选中」一律用 `.empty`，判断时用 `!= .empty` / `== .empty`，杜绝「位置 0 的空区间」与「无区间」混淆。

**`RimeStringSlice.asString` 的裁剪流程：**

```text
asString
 ├─ 1. guard let ptr = str else { return nil }   指针为空 → nil
 ├─ 2. Data(bytes: ptr, count: Int(length))      严格按 .length 取字节
 └─ 3. String(data:encoding:.utf8)               按 UTF-8 解码
```

**`NSPoint` 几何运算的典型用法（向量归一化）：**

```text
sign(number):
  if number.length >= 2 { return number / number.length }  // 单位向量
  else                  { return number / 2 }
```

#### 4.4.3 源码精读

`NSRange.empty` 的定义 [sources/BridgingFunctions.swift:58-60](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L58-L60)：

```swift
extension NSRange {
  static let empty = NSRange(location: NSNotFound, length: 0)
}
```

它就是「`location` 设为 `NSNotFound`、长度 0」的别名。作为默认值出现在 [sources/SquirrelInputController.swift:17](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L17) `private var selRange: NSRange = .empty` 与 [sources/SquirrelPanel.swift:23](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelPanel.swift#L23)、[sources/SquirrelView.swift:31](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L31)；使用前守卫在 [sources/SquirrelView.swift:79](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L79) `guard range != .empty else { return nil }`。上屏时也用它表示「不指定替换范围」：[sources/SquirrelInputController.swift:553](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L553) `client.insertText(string, replacementRange: .empty)`。

`RimeStringSlice.asString` 定义在 AppDelegate 文件末尾 [sources/SquirrelApplicationDelegate.swift:253-263](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L253-L263)：

```swift
extension RimeStringSlice {
  /// Bridge the slice's pointer + length to a Swift String, honoring `.length`.
  /// librime clips `.length` to the first Unicode character for abbreviated labels
  /// when no explicit `abbrev:` field is defined, so reading past `.length` (e.g. with
  /// `String(cString:)`) would incorrectly return the full `states:` value.
  var asString: String? {
    guard let ptr = str else { return nil }
    let data = Data(bytes: UnsafeRawPointer(ptr), count: Int(length))
    return String(data: data, encoding: .utf8)
  }
}
```

注释本身就把「为什么不能用 `String(cString:)`」讲清楚了：引擎会把缩写标签的 `.length` 截到首个字形，越过 `.length` 读会拿到完整的 `states:` 值。真实使用点是读状态图标标签 [sources/SquirrelApplicationDelegate.swift:298](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L298) `return stateLabelShort.asString` 与 [sources/SquirrelInputController.swift:183](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L183)。

`NSPoint` 几何扩展 [sources/BridgingFunctions.swift:62-83](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L62-L83)：

```swift
extension NSPoint {
  static func += (lhs: inout Self, rhs: Self) { lhs.x += rhs.x; lhs.y += rhs.y }
  static func - (lhs: Self, rhs: Self) -> Self { Self.init(x: lhs.x - rhs.x, y: lhs.y - rhs.y) }
  static func -= (lhs: inout Self, rhs: Self) { lhs.x -= rhs.x; lhs.y -= rhs.y }
  static func * (lhs: Self, rhs: CGFloat) -> Self { Self.init(x: lhs.x * rhs, y: lhs.y * rhs) }
  static func / (lhs: Self, rhs: CGFloat) -> Self { Self.init(x: lhs.x / rhs, y: lhs.y / rhs) }
  var length: CGFloat { sqrt(pow(self.x, 2) + pow(self.y, 2)) }
}
```

`.length` 是向量模长 \(\sqrt{x^2 + y^2}\)。真实用法在绘制圆角时的向量归一化 [sources/SquirrelView.swift:346-352](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L346-L352)：

```swift
func sign(_ number: NSPoint) -> NSPoint {
  if number.length >= 2 {
    return number / number.length   // 归一化为单位向量
  } else {
    return number / 2
  }
}
```

即把一个方向向量除以自身模长，得到单位方向向量。若没有 `*`、`/`、`.length` 这些扩展，这行就得写成 `NSPoint(x: number.x / number.length, y: number.y / number.length)`，可读性差很多。

#### 4.4.4 代码实践

**实践目标**：用一个 Swift Playground 风格的思想实验，确认 `NSRange.empty` 与「位置 0 的空区间」不等价，并理解 `asString` 的裁剪必要性。

1. 打开 [sources/BridgingFunctions.swift:58-60](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/BridgingFunctions.swift#L58-L60)，确认 `NSRange.empty` 的 `location` 是 `NSNotFound`（不是 0）。
2. 追踪一处守卫 [sources/SquirrelView.swift:79](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelView.swift#L79) `guard range != .empty else { return nil }`，理解它拦掉「无区间」后，后续才放心地把 `range.location` 当成合法下标用。
3. **思想实验（不修改源码）**：假设把 `selRange` 的默认值从 `.empty` 改成 `NSRange(location: 0, length: 0)`，会怎样？——「无选中」与「在第 0 字符处选中了 0 个字」再也分不清，守卫 `range != .empty` 永远为假（会误把「无选中」当成「在第 0 位有合法区间」继续往下走），绘制高亮时可能在错误位置画出一个零宽高亮。
4. 打开 [sources/SquirrelApplicationDelegate.swift:253-263](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L253-L263)，对照注释，复述「为什么读 `RimeStringSlice` 必须用 `.asString` 而不是 `String(cString:)`」。
5. **预期结果**：你能讲清三点——`NSRange.empty` 是「无区间」哨兵、与位置 0 的空区间严格区分；`asString` 严格按 `.length` 截取避免读到截断点之后的完整文本；`NSPoint` 几何运算符只是把 `NSPoint(x:,y:)` 样板压成数学式写法。

#### 4.4.5 小练习与答案

**练习 1**：`NSRange.empty` 用 `NSNotFound` 作 `location`，为什么不直接用 `NSRange(location: 0, length: 0)` 表示「空」？

> **参考答案**：`NSRange(location: 0, length: 0)` 表示「在第 0 个字符处有一个长度为 0 的区间」——这是一个有意义的状态（光标停在开头），并不是「没有区间」。Cocoa 的 `NSNotFound` 是「找不到/不存在」的哨兵值，用 `(NSNotFound, 0)` 才能确切表达「根本没有选中区段」。混用会导致守卫失效、在错误位置绘制零宽高亮。

**练习 2**：状态图标标签用 `RimeStringSlice.asString`，候选定稿文本用 `String(cString:)`，为什么桥接方式不同？

> **参考答案**：候选定稿文本（`RimeCommit.text`）是裸 `UnsafePointer<CChar>`，以 `\0` 结尾，`String(cString:)` 读到 `\0` 停下，正好取到完整文本。状态标签是 `RimeStringSlice`（带 `str` 指针 + `length` 长度），引擎生成缩写标签时会把 `.length` 截到首个字形，`String(cString:)` 会越过 `.length` 一直读到 `\0`、误取完整的 `states:` 值。`asString` 用 `Data(bytes:count: Int(length))` 严格按 `.length` 截取，才能拿到引擎想给的缩写。

## 5. 综合实践

把本讲四个模块串起来，跟踪一次「`get_commit` 取回定稿文本并上屏」的完整内存生命线。这条链路同时用到了 `rimeStructInit`、`asString`/`String(cString:)` 的区分、`NSRange.empty`，以及与 `setCString` 的所有权对比。

**实践目标**：画出从「声明结构」到「释放结构」的内存所有权时序，标注每块 C 内存的分配者与释放者。

**操作步骤**：

1. 打开 [sources/SquirrelInputController.swift:427-433](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L427-L433)，把 `rimeConsumeCommittedText` 的每一步对号入座：
   - `var commitText = RimeCommit.rimeStructInit()` → 清零 + 填 `data_size`（本讲 4.1）。
   - `rimeAPI.get_commit(session, &commitText)` → 引擎在 C 堆上 `malloc` 出 `commitText.text`。
   - `commit(string: String(cString: text))` → 用 `String(cString:)` 读裸 `char*`（本讲 4.4，与 `.asString` 的区别）。
   - `rimeAPI.free_commit(&commitText)` → 前端替引擎释放那块 `malloc`。
2. 打开 [sources/SquirrelInputController.swift:553](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L553)，确认上屏用的 `replacementRange: .empty` 就是本讲 4.4 的哨兵（「不指定替换范围，插在光标处」）。
3. **画一张所有权对照表**（纸上或文本），比较两种方向：

   | 内存块 | 分配者 | 释放者 | 对应代码 |
   | --- | --- | --- | --- |
   | `setCString` 的 `strdup` 串（如 `app_name`） | 前端 `strdup` | 覆盖时 `setCString` 自带 `free` / 进程退出 | `setupRime` |
   | `get_commit` 填入的 `commitText.text` | 引擎 `malloc` | 前端 `free_commit` | `rimeConsumeCommittedText` |

4. **自检提问**：如果删掉第 432 行的 `free_commit`，每次上屏都会泄漏一块 C 字符串——为什么？如果删掉 `setCString` 里的 `if let existing ... free(...)`，对同一字段反复赋值会怎样？

**需要观察的现象**：你能不看源码，复述出「Swift→C 用 `setCString` 自管 `strdup`/`free`；C→Swift 用 `get_*` + `free_*` 配对」，并指出二者是镜像方向。

**预期结果**：你画出一张清晰的所有权时序图，标注 `rimeStructInit`（清零 + `data_size`）、引擎填充、`String(cString:)` 读取、`free_commit` 释放四个节点，并能解释为什么 `setCString` 与 `free_commit` 的释放责任落在不同主体上。

> 说明：本综合实践为「源码阅读型实践」，不要求运行程序。若需运行验证，需要完整的 macOS + librime 构建环境，并启用 Xcode 的内存图（Debug Memory Graph）或 Address Sanitizer 观察 `free_commit` 缺失时的泄漏，具体表现「待本地验证」。

## 6. 本讲小结

- `rimeStructInit()` 同时做两件 Swift 默认 `init()` 做不到的事：用 `memset` 把整块 C 结构内存（含 padding、指针字段）清零，并按 librime 约定回填 `data_size` 这个「ABI 尺寸书签」——引擎据此判断结构体版本与可安全访问的字节范围，`data_size` 错或漏则引擎拒绝填充。
- `setCString` 用 `strdup` 把 Swift 字符串拷成 C 堆上的长期副本写进字段（因为 `withCString` 的临时缓冲出了闭包就失效）；所有权归结构体字段，覆盖同一字段时自带 `free` 回收旧值，一次性初始化的字符串随引擎 `finalize`/进程退出而结束。
- `setCString`（Swift→C，前端 `strdup`/前端 `free`）与 `get_*`+`free_*`（C→Swift，引擎 `malloc`/前端替引擎 `free`）是一对镜像方向的所有权约定，任何一边漏释放都会泄漏。
- `?=` 是项目自定义的「可选赋值」运算符（`AssignmentPrecedency`），语义是「右侧非 `nil` 才赋值」；两个重载分别面向非可选左值（配置覆盖默认）与可选左值（刷新 `weak` client），且永远不会把可选左值主动清成 `nil`。
- `NSRange.empty`（`location = NSNotFound`）是「无区间」哨兵，与「位置 0 的空区间」严格区分；`RimeStringSlice.asString` 严格按 `.length` 截取以避免读到截断点之后的完整 `states:` 值；`NSPoint` 几何运算符（`+=`/`-`/`*`/`/`/`.length`）把二维向量运算压成数学式写法。

## 7. 下一步学习建议

- **回顾与印证**：回到 [u2-l2 全局 librime 初始化](u2-l2-global-rime-init.md)，对照 `setupRime` 看 `rimeStructInit` + `setCString` 在真实初始化里的位置；回到 [u2-l6 rimeUpdate 数据流](u2-l6-rime-update-dataflow.md)，对照「三段式配对释放」看 `get_*` / `free_*` 的镜像所有权。
- **桥接的另一面：回调**：本讲只讲了「Swift 调 C」与「C 填结构给 Swift」。反向的「C 回调 Swift」用的是 `@convention(c)` 闭包 + `Unmanaged` 裸指针，那是 [u5-l3 保留属性：插件→前端协调](u5-l3-reserved-property.md) 里 `notificationHandler` 的主题，建议接着读，凑齐 Swift↔C 双向桥接的全貌。
- **打包与安装**：理解了桥接约定后，可以看 [u5-l5 打包、安装与 Sparkle 更新](u5-l5-packaging-installer.md)，了解这些 Swift 代码最终如何被签名、公证、注册成系统输入法。
- **自己动手（可选）**：在本地 macOS + librime 环境下，用 Xcode 的 Debug Memory Graph 验证「删掉某个 `free_*` 会产生一块泄漏」，把本讲的所有权结论从「读懂」推进到「看见」。
