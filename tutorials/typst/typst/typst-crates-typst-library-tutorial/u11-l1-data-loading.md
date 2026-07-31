# 数据加载：read / csv / json / toml / yaml / cbor / xml

## 1. 本讲目标

Typst 在排版时经常需要「从外部取数据」：读一段 HTML 片段、把一份 JSON 配置展开成表格、用 CSV 画图、从 YAML 里取作者列表……这些需求由 `src/loading/` 模块统一提供。本讲结束后，你应当能够：

- 说清 **path 与 bytes 两种数据源如何被统一抽象**（`DataSource` → `Load` → `Loaded`）；
- 解释 `Loaded` 为什么要携带「来源信息」（`LoadSource`），以及它如何决定一条解析错误的 span 落在「外部文件」还是「调用处」；
- 看懂 `read / json / yaml / toml / cbor / csv / xml` 七个函数各自的三段式结构（加载字节 → 解析 → 错误归位），并理解它们为何几乎长得一样；
- 认识到 json/yaml/toml/cbor 之所以能解析成 Typst `Value`，靠的是 `Value` 自身实现的 serde `Deserialize`（一座「转换桥」），而非每个函数手写映射。

## 2. 前置知识

本讲承接两篇前置讲义：

- **u5-l1 World trait 与资源加载**：`World` 是编译器与外部环境的接口，其 `file(id)` 方法负责按 `FileId` 取回文件字节，且返回的 `Bytes` 是引用计数类型，克隆近乎免费。数据加载的「path 分支」最终就是调 `engine.world.file(...)`。
- **u2-l2 容器类型 Array、Dict、Bytes 与 Label**：`Bytes` 是 `Arc<LazyHash<dyn Bytelike>>` 的引用计数字节视图；`Array`/`Dict` 是 Typst 的列表与字典容器。本讲里所有「结构化数据」最终都被还原成这三个值。

还需要两个背景概念：

- **serde**：Rust 生态里事实标准的「序列化/反序列化」框架。一个类型只要实现 `serde::Deserialize`，就能从 JSON / YAML / TOML / CBOR 等任意一种「解码器（Deserializer）」还原出来。Typst 的 `Value` 自己实现了 `Deserialize`，所以四种格式复用同一座桥。
- **`Spanned<T>`**：来自 `typst-syntax`，把一个值 `T` 和它的源码位置 `Span` 绑在一起。数据加载函数的入参几乎都是 `Spanned<...>`，目的是解析失败时能把错误指到用户写 `json("...")` 的那一行。

最后回顾一条贯穿全 crate 的主线（见 u5-l1/u5-l4）：**typst-library 只负责「定义与归一化」，真正的 IO 行为通过 `Engine.world` 与 `Routines` 回调到别的 crate**。本讲同样如此——加载逻辑留在本 crate，读文件的实现由 `World` 的宿主提供。

## 3. 本讲源码地图

`src/loading/` 目录结构很扁平，七个格式各占一个文件，`mod.rs` 是公共抽象层：

| 文件 | 作用 |
| --- | --- |
| [src/loading/mod.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/mod.rs) | 公共抽象：`DataSource` / `Load` trait / `Loaded` / `Readable` / `LoadSource`，以及把七个函数注册进标准库的 `define` |
| [src/loading/read.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/read.rs) | `read()`：读纯文本或原始字节 |
| [src/loading/json.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/json.rs) | `json()` + `json.encode()` |
| [src/loading/yaml.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/yaml.rs) | `yaml()` + `yaml.encode()` |
| [src/loading/toml.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/toml.rs) | `toml()` + `toml.encode()` |
| [src/loading/cbor.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/cbor.rs) | `cbor()` + `cbor.encode()`（二进制格式） |
| [src/loading/csv.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/csv.rs) | `csv()`：二维表 → 数组/字典数组 |
| [src/loading/xml.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/xml.rs) | `xml()`：XML 树 → 嵌套字典/字符串 |

此外两个「桥接」位置不在 `loading/` 下，但本讲会用到：

- [src/foundations/value.rs:368-493](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/value.rs#L368-L493)：`Value` 的 serde `Deserialize` 实现，四种格式共用的转换桥。
- [src/diag.rs:761-844](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L761-L844)：`LoadError` 与 `LoadedWithin::within`，负责把「无位置的解析错误」补上正确的 span。

## 4. 核心概念与源码讲解

### 4.1 统一抽象层：DataSource / Load / Loaded / Readable

#### 4.1.1 概念说明

七个加载函数面对的第一个问题是：**用户可能传一个文件路径（字符串），也可能直接传一段字节（`bytes`）**。例如 `json("data.json")` 和 `json(bytes(...))` 都应被接受。如果每个函数各自处理这两种情况，就会有七份重复代码。

`loading/mod.rs` 的做法是抽出一组小而精的类型：

- `DataSource`：枚举「数据从哪来」，只有两个变体——`Path`（路径）或 `Bytes`（现成字节）。
- `Load` trait：把 `DataSource` 真正变成字节的动作，输入是 `World`，输出是 `Loaded`。
- `Loaded`：加载结果，**除了字节本身，还附带「这段字节是从哪种来源来的」**。
- `LoadSource`：来源标记，`Path(FileId)` 或 `Bytes`，专供错误归位用。
- `Readable`：`read()` 专用的输出类型，表示「要么是解码后的字符串，要么是原始字节」。

这组抽象的关键设计意图是：**把「取字节」和「解析字节」彻底解耦**。所有格式函数只需写 `let loaded = source.load(engine.world)?;`，就拿到了带来源标记的字节，剩下的解析逻辑彼此独立。

#### 4.1.2 核心流程

一次数据加载的通用流程：

```
用户调用 json("a.json") 或 json(some_bytes)
        │
        │  source: Spanned<DataSource>  (cast! 把 str/bytes 自动包成对应变体)
        ▼
source.load(engine.world)        ◄── Load trait
        │
        ├── 若 DataSource::Path  → resolve_if_some 解析路径
        │                         → world.file(file_id) 取字节
        │                         → LoadSource::Path(file_id)
        ├── 若 DataSource::Bytes → 直接 clone 字节
        │                         → LoadSource::Bytes
        ▼
Loaded { source: Spanned<LoadSource>, data: Bytes }
        │
        ▼
交给具体格式解析（serde / csv / roxmltree）
        │
        ▼
解析失败时：err.within(&loaded)   ◄── 用 LoadSource 决定 span 落点
```

注意 `Load` 是被多次实现的 trait，针对「单个源」「单个源的引用」「多个源」三种入参各有一份 impl，输出分别是 `Loaded` 与 `Vec<Loaded>`。

#### 4.1.3 源码精读

先看总装入口 `define`，它把七个函数注册到 `DataLoading` 分类下（分类机制见 u1-l2/u1-l3）：

[src/loading/mod.rs:34-44](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/mod.rs#L34-L44) —— `define` 用 `start_category`/`reset_category` 把七个 `define_func` 包成一个分类区间，逐个注册 `read/csv/json/toml/yaml/cbor/xml`。

`DataSource` 是一切入参的统一形态：

[src/loading/mod.rs:46-63](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/mod.rs#L46-L63) —— `DataSource` 枚举只有 `Path(PathOrStr)` 与 `Bytes(Bytes)` 两个变体；下方的 `cast!` 块声明：用户传 `str`/路径会被包成 `Path`，传 `bytes` 会被包成 `Bytes`。这就是「两种数据源统一抽象」的落点。

`PathOrStr` 本身也是二选一（路径对象或字符串），它的 `resolve_if_some` 负责把相对路径锚定到当前文件所在目录：

[src/foundations/path.rs:181-214](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/path.rs#L181-L214) —— `resolve_if_some` 在 `within` 为 `None` 时报 "cannot access file system from here"，这正是「无文件上下文时不能读路径」的来源。

`Load` trait 与最关键的「单源」实现：

[src/loading/mod.rs:65-72](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/mod.rs#L65-L72) —— trait 只有一个方法 `load(&self, world)`，关联类型 `Output` 表示返回单个还是多个结果。

[src/loading/mod.rs:82-112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/mod.rs#L82-L112) —— 这是 path 与 bytes 的**真正分叉点**，值得逐行读懂：

- `Path` 分支：`path.resolve_if_some(self.span.id())` 把路径解析成绝对路径并 `.intern()` 成 `FileId`；`world.file(resolved)` 真正取字节（见 u5-l1）；若用户给的字符串以 `http://`/`https://` 开头，额外补一条 "network access is not supported" 提示；最终 `LoadSource::Path(resolved)` 记下文件身份。
- `Bytes` 分支：字节已现成，直接 `data.clone()`，来源标记为 `LoadSource::Bytes`。

> 注意 `world.file(...)` 失败时用了一个 `map_err` 闭包：把原始错误转成 `HintedString`，判断到 URL 串就追加网络提示，再 `.at(self.span)` 补上调用处 span。这呼应 u5-l3 的「底层报字符串、上层补 span」两段式。

「多源」实现把每个子源各调一次单源加载：

[src/loading/mod.rs:122-132](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/mod.rs#L122-L132) —— `OneOrMultiple<DataSource>` 表示「一个或一数组源」，对 `Vec` 里每个源 map 一次 `load`。本讲的七个函数都不用它；它的消费者是 `#bibliography(sources: ...)`（可传多个 `.bib`）与 raw 语法文件加载（见 `src/model/bibliography.rs`、`src/text/raw.rs`）。

加载结果 `Loaded` 与来源标记 `LoadSource`：

[src/loading/mod.rs:134-154](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/mod.rs#L134-L154) —— `Loaded { source, data }`；`LoadSource::Path(FileId)` 携带文件身份、`Bytes` 不带。这个标记唯一用途是 **4.3 节的错误归位**：解析失败时决定 span 指向外部文件还是调用处。

`Readable` 是 `read()` 的专属输出：

[src/loading/mod.rs:156-186](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/mod.rs#L156-L186) —— `Readable = Str | Bytes`，提供 `into_bytes`（字符串降回字节）与 `into_source`（包装回 `DataSource::Bytes`）。

#### 4.1.4 代码实践

**实践目标**：亲手追踪 `Load` trait 的实现链，验证「path 与 bytes 两种数据源在同一个 `load` 调用里被统一」。

**操作步骤**（纯源码阅读型）：

1. 打开 [src/loading/mod.rs:82-112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/mod.rs#L82-L112)。
2. 在 `Path` 分支里找到「真正取字节」的那一行，记下它调用了 `World` 的哪个方法。
3. 在 `Bytes` 分支里确认它**没有**调用 `World`，只是 `clone`。
4. 对比 [src/loading/mod.rs:46-63](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/mod.rs#L46-L63) 的 `cast!`，确认用户层传 `str` 与传 `bytes` 分别落到哪个变体。

**需要观察的现象 / 预期结果**：

- 两种来源最终都产出同一个 `Loaded { source, data }` 结构，差别仅在 `source` 字段是 `Path(_)` 还是 `Bytes`。
- `Path` 分支多做了「路径解析 + 文件读取 + URL 提示」三件事；`Bytes` 分支几乎零成本。
- 结论：**统一发生在 `Loaded` 这一层**，下游解析器从此只认 `Loaded.data`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Load` trait 要为 `Spanned<DataSource>`、`Spanned<&DataSource>`、`Spanned<OneOrMultiple<DataSource>>` 分别写 impl，而不是只写一个？

> **答案**：因为输出类型不同——单源返回 `Loaded`，多源返回 `Vec<Loaded>`；而「值」与「引用」两份 impl 是为了调用方既能交出所有权、也能只借引用（`Spanned<DataSource>::load` 内部其实就是 `self.as_ref().load(world)`，委托给引用版本，见 [mod.rs:74-80](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/mod.rs#L74-L80)）。

**练习 2**：`LoadSource::Path(FileId)` 里的 `FileId` 最终被谁用掉？

> **答案**：被错误归位逻辑用掉。当解析失败时（见 4.3），`LoadedWithin::within` 会根据 `Path(file_id)` 把错误 span 指到外部文件内部的具体行列（见 [diag.rs:859-876](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L859-L876)）；若是 `Bytes` 则指回调用处 span。

---

### 4.2 read()：文本与字节的薄封装

#### 4.2.1 概念说明

`read()` 是七个函数里最简单的一个：它只负责「把文件内容读出来」，不做任何结构化解析。返回值要么是 UTF-8 字符串（默认），要么是原始字节（`encoding: none`）。

值得特别注意的反差是：**`read()` 的入参是 `PathOrStr`，不是 `DataSource`**——也就是说 `read()` 只接受路径，不接受现成的 `bytes`。而 `json/csv/...` 六个函数接受 `DataSource`（路径或字节都行）。这是一个有意的区分：`read` 的语义就是「读文件」，而你已经握着字节时自然不需要它。

#### 4.2.2 核心流程

```
read("a.html", encoding: ?)
   │  path: Spanned<PathOrStr>
   │  path.map(DataSource::Path).load(world)   ◄── 包装成 Path 源再走统一加载
   ▼
Loaded { data: Bytes }
   │  按 encoding 分支
   ├── None            → Readable::Bytes(data)        原样返回字节
   └── Some(Utf8)      → data.to_str() → Readable::Str 解码为字符串（失败报 UTF-8 错）
```

#### 4.2.3 源码精读

[src/loading/read.rs:23-40](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/read.rs#L23-L40) —— 注意三处：

1. 入参 `path: Spanned<PathOrStr>`（不是 `DataSource`）；
2. 第一行 `path.map(DataSource::Path).load(engine.world)?` 把路径包成 `DataSource::Path` 后复用 4.1 的统一加载链——`read` 不重复造轮子；
3. `encoding` 决定输出：`None` 直接给 `Readable::Bytes`，`Some(Utf8)` 调 `loaded.data.to_str()` 并 `.within(&loaded)` 把 UTF-8 解码错误归位到外部文件。

[src/loading/read.rs:42-47](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/read.rs#L42-L47) —— `Encoding` 枚举当前只有 `Utf8` 一个变体，留作未来扩展点（比如其它编码）。

#### 4.2.4 代码实践

**实践目标**：通过修改 `encoding` 参数理解字符串与字节两条返回路径。

**操作步骤**：

1. 阅读函数文档里的示例 [src/loading/read.rs:8-22](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/read.rs#L8-L22)，它演示了 `read("example.html")`（读文本）与 `read("tiger.jpg", encoding: none)`（读二进制）两种用法。
2. 准备一个最小 Typst 文档：
   ```typst
   #let text = read("hello.txt")          // 预期：返回 string
   #let raw = read("hello.txt", encoding: none)  // 预期：返回 bytes
   #text \n #raw
   ```
3. 把 `hello.txt` 写成一段含中文的 UTF-8 文本，再故意存成非 UTF-8（如 GBK）观察报错。

**需要观察的现象 / 预期结果**：

- 默认调用得到 `string`，`encoding: none` 得到 `bytes`；
- 非 UTF-8 文件在默认模式下报 "file is not valid UTF-8"，且错误指到 `hello.txt` 内部的具体字节位置（因为 `.within(&loaded)` 走的是 `Path` 归位路径）。

> 运行结果「待本地验证」——本讲只确认源码逻辑，未替你执行 typst。

#### 4.2.5 小练习与答案

**练习 1**：既然 `read` 的第一步就是把 `PathOrStr` 包成 `DataSource::Path`，为什么不直接让 `read` 也收 `DataSource`？

> **答案**：语义取舍。`read` 的定位是「从文件读」，允许传 `bytes` 会与「你已经有了字节」的自相矛盾场景混淆；且文档承诺的是「读文件」，收紧入参让接口更直白。其它六个解析函数需要支持「直接解析一段网络/用户输入的字节」，所以才用更宽的 `DataSource`。

**练习 2**：`encoding` 参数的默认值是什么？它是位置参数还是命名参数？

> **答案**：默认 `Some(Encoding::Utf8)`，且是 `#[named]` 命名参数（[read.rs:31-33](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/read.rs#L31-L33)），所以用户必须写 `encoding: none` 而不能位置传入。

---

### 4.3 serde 家族：json / yaml / toml / cbor（解析 + 编码 + 错误归位）

#### 4.3.1 概念说明

`json`、`yaml`、`toml`、`cbor` 四个函数处理的是「键值/容器型结构化数据」。它们的源码结构惊人地一致，都遵循一个三段式模板：

```
let loaded = source.load(engine.world)?;     // ① 取字节（4.1 的统一链）
<格式解析器>(loaded.data...)                  // ② 用第三方库解析
    .map_err(format_X_error)                  //    把库错误格式化成 LoadError
    .within(&loaded)                          // ③ 补 span，归位到外部文件或调用处
```

这四个函数能直接产出 Typst `Value`，靠的不是各自手写一份「JSON 类型 → Typst 类型」的映射表，而是 **`Value` 自身实现了 serde 的 `Deserialize`**。四种格式只是四把不同的「解码器」，喂给同一个 `ValueVisitor`。这是一座共享的转换桥，也是本节最重要的认知。

四个函数还各自带一个 `encode` 子函数（挂在 `#[scope] impl` 下，见 u3-l4），把 Typst 值序列化回对应格式的字符串（cbor 是字节）。`read`、`csv`、`xml` 没有 encode——因为它们没有「一一对应的序列化形态」。

四个函数的细微差别：

| 函数 | 解析库 | 输入处理 | 返回类型 | encode 返回 |
| --- | --- | --- | --- | --- |
| `json` | `serde_json` | `from_slice`（字节） | `Value` | `Str` |
| `yaml` | `serde_yaml` | `from_slice`（字节） | `Value` | `Str` |
| `toml` | `toml` | 先 `as_str` 再 `from_str` | `Dict` | `Str` |
| `cbor` | `ciborium` | `from_reader`（字节流） | `Value` | `Bytes` |

#### 4.3.2 核心流程

以 `json()` 为代表：

```
json(source: Spanned<DataSource>)
   │
   ▼ loaded.data (Bytes)
   ├── 检查 UTF-8 BOM（json 特有）→ 若有则友好报错
   ▼
serde_json::from_slice(raw)
   │  raw 反序列化的目标类型由返回类型推断为 Typst Value
   │  → 走 Value::deserialize → deserialize_any → ValueVisitor
   │     visit_bool→bool, visit_i64→int, visit_f64→float,
   │     visit_str→str, visit_seq→Array, visit_map→Dict, visit_unit→none …
   ▼
Ok(Value) | Err(serde 错误)
   │  Err 经 format_json_error → LoadError
   ▼
.within(&loaded)  → SourceDiagnostic（span 落到外部文件或调用处）
```

错误归位（`LoadedWithin::within`）的逻辑值得单独理解：解析库返回的错误本身**没有 Typst span**（它根本不知道 Typst 的存在）。`within(&loaded)` 根据 `loaded.source` 决定把 span 放哪——`Path(file_id)` 时 span 指到外部文件里出错的具体行列，`Bytes` 时 span 退回到 `loaded.source.span`（即用户写 `json(...)` 的调用处）。

#### 4.3.3 源码精读

**`json()` 函数体**：

[src/loading/json.rs:100-128](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/json.rs#L100-L128) —— 注意三段式：`source.load` → BOM 检查 → `serde_json::from_slice(raw)` + `map_err(format_...)` + `.within(&loaded)`。其中 [json.rs:110-120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/json.rs#L110-L120) 的 BOM 检查是 json 独有的友好处理：文件以 UTF-8 BOM 开头时直接报 "unexpected Byte Order Mark" 并提示 "JSON requires UTF-8 without a BOM"。

`json` 的 `encode` 子函数（`#[scope] impl json`）：

[src/loading/json.rs:130-152](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/json.rs#L130-L152) —— `encode(value, pretty:)` 调 `serde_json::to_string_pretty` 或 `to_string`，正向走 `Value` 的 `Serialize`。`pretty` 默认为 `true`。

**`yaml()`** 与 json 几乎一模一样，只是换了库：

[src/loading/yaml.rs:95-105](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/yaml.rs#L95-L105) —— `serde_yaml::from_slice`，错误经 [format_yaml_error](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/yaml.rs#L124-L134) 用 `error.location()` 抽出行列转成 `ReportTextPos`。

**`toml()`** 多一步「先解码成字符串」：

[src/loading/toml.rs:92-101](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/toml.rs#L92-L101) —— 因为 `toml` 库的 API 是 `from_str`，所以先 `loaded.data.as_str().within(&loaded)?` 拿到 `&str` 再解析；返回类型是 `Dict`（TOML 顶层恒为表）。

**`cbor()`** 是二进制格式，没有「行列」位置：

[src/loading/cbor.rs:73-83](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/cbor.rs#L73-L83) —— `ciborium::from_reader`；其 [format_cbor_error](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/cbor.rs#L88-L98) 用的是 `LoadError::binary`（不带 `text_pos`），因为二进制流没有有意义的行列。

**转换桥：`Value` 的 `Deserialize`** —— 这是四种格式共用的小型映射表：

[src/foundations/value.rs:368-375](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/value.rs#L368-L375) —— `Value::deserialize` 用 `deserialize_any(ValueVisitor)`，意味着「让解码器自己判断遇到的是什么类型，再回调对应的 visit 方法」。

[src/foundations/value.rs:380-493](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/value.rs#L380-L493) —— `ValueVisitor` 把每种 serde 原语映射成 Typst 值：

- `visit_bool` → `bool`；`visit_i*`/`visit_u*`（含 i128/u128）→ `int`；`visit_f64` → `float`；
- `visit_str`/`visit_string` → `str`；`visit_bytes` → `Bytes`；
- `visit_unit`/`visit_none` → `Value::None`（对应 JSON `null`）；
- `visit_seq` → `Array`；`visit_map` → `Dict`，并且 [value.rs:486-492](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/value.rs#L486-L492) 有一处 TOML 专属特判：解析出的字典若是 TOML 的 datetime 形状（`Datetime::from_toml_dict`），就转成 `datetime` 值而不是普通字典。

> 这就是「json/yaml/toml/cbor 长得一样」的根本原因——它们共用这座桥，差异只在「用哪把解码器」和「错误怎么格式化」。

**错误归位 `LoadedWithin::within`**：

[src/diag.rs:761-795](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L761-L795) —— `LoadError` 故意**只存 `text_pos` 与 `message`，不存 span/FileId**。注释（[diag.rs:755-757](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L755-L757)）解释了原因：避免把源文件的 `Span`/`FileId` 污染进 comemo 记忆化（见 u5-l1、u9-l3）。`text` 构造器带位置、`binary` 不带位置。

[src/diag.rs:811-844](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L811-L844) —— `LoadedWithin` 是一个把 `LoadError`（或 `Result<_, LoadError>`）补上 `Loaded` 上下文、转成 `SourceDiagnostic`（或 `SourceResult<T>`）的 trait。它的 `within` 方法会按 `loaded.source` 分派到 [load_err_in_text](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L848-L886)（`Path` 分支：把行列换算成文件内字节区间做 span）或 `load_err_in_binary`（`Bytes` 分支：span 退回调用处）。

#### 4.3.4 代码实践

**实践目标**：追踪 `json()` 的解析结果如何变成 Typst `Value`，体会「共用转换桥」。

**操作步骤**（源码追踪型）：

1. 从 [src/loading/json.rs:122-128](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/json.rs#L122-L128) 的 `serde_json::from_slice(raw)` 出发。
2. 注意该函数返回 `SourceResult<Value>`，所以 `from_slice` 的反序列化目标被推断为 Typst `Value`。
3. 跳到 [src/foundations/value.rs:368-375](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/value.rs#L368-L375)，确认 `Value::deserialize` 用 `deserialize_any`。
4. 在 [value.rs:380-493](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/value.rs#L380-L493) 里，逐个核对你将 JSON `{"a": 1, "b": [true, null]}` 喂进去会经过哪几个 visit 方法（答案：visit_map → 其内 visit_str("a")+visit_i64(1)、visit_str("b")+visit_seq[visit_bool, visit_unit]）。
5. 额外实验（待本地验证）：写一段 Typst 测试 encode 往返——`#json.encode(json(`{"a":1}`))` 应当输出形如 `{ "a": 1 }` 的字符串，验证 `Value` 同样实现了 `Serialize`。

**需要观察的现象 / 预期结果**：

- 整条链没有任何「JSON 专属的类型映射代码」——映射全部集中在 `ValueVisitor`，json/yaml/toml/cbor 共用。
- 把同一个 `Value` 喂给 `json.encode` 与 `yaml.encode`，会得到同一值的不同文本表示，证明桥是双向的。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `LoadError` 故意不存 `Span`/`FileId`？

> **答案**：为了避免污染 comemo 的记忆化缓存（[diag.rs:755-757](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L755-L757)）。span/FileId 来自源文件、会随编辑频繁变化；如果错误对象携带它们，每次编辑都会让相关 comemo 缓存失效。所以解析期用「无位置的 `LoadError`」，只在最后用 `within(&loaded)` 现场补 span。

**练习 2**：JSON 文档里的 `null` 最终变成哪个 Typst 值？依据是哪一行？

> **答案**：变成 `none`。serde 把 `null` 当作 unit，`ValueVisitor::visit_unit` 返回 `Ok(Value::None)`（[value.rs:478-480](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/value.rs#L478-L480)）。

**练习 3**：`toml()` 的返回类型是 `Dict` 而非 `Value`，为什么？

> **答案**：TOML 规范规定顶层必须是 table，所以解析结果恒为字典；把返回类型写死成 `Dict` 既是对规范的反映，也让用户拿到的直接是字典而非需要再拆包的 `Value`（[toml.rs:92-101](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/toml.rs#L92-L101)）。

---

### 4.4 csv() 与 xml()：表格与树形

#### 4.4.1 概念说明

`csv` 与 `xml` 处理的是两种「不那么键值」的格式，所以它们**不走 serde 桥**，各自手写转换：

- **`csv`** 把文件解析成二维表，每行要么是一个字符串数组（`array`），要么是「表头键 → 字符串」的字典（`dictionary`）。用 `csv` crate（Rust 生态的 CSV 库）。
- **`xml`** 把文件解析成节点树，元素节点变成带 `tag`/`attrs`/`children`（以及 `namespace`）的字典，文本节点变成字符串。用 `roxmltree` 库。

二者同样遵循「`source.load` → 解析 → `.within(&loaded)` 归位」的大框架，只是解析步骤换成手写循环或递归。

#### 4.4.2 核心流程

`csv()`：

```
csv(source, delimiter: ',', row_type: array|dictionary)
   │  loaded.data → csv::ReaderBuilder
   ├── row_type == Dict → has_headers=true，先读一行作 headers，line_offset=2
   └── row_type == Array → has_headers=false，line_offset=1
   ▼ 逐行 records()
   每行：
   ├── Dict 模式 → headers.zip(row) 组成 Dict
   └── Array 模式 → row 收成 Array<str>
   ▼ 收集成 Array（行数组）
```

`xml()`：

```
xml(source)
   │  loaded.data.as_str() → roxmltree::Document
   ▼ convert_xml(root) 递归
   节点：
   ├── 文本节点 → str
   ├── 根节点   → Array（所有顶层子节点的数组）
   └── 元素节点 → Dict { namespace, tag, attrs: Dict, children: Array }
```

#### 4.4.3 源码精读

**`csv()`**：

[src/loading/csv.rs:26-92](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/csv.rs#L26-L92) —— 关键点：

- [csv.rs:49-57](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/csv.rs#L49-L57) 用 `ReaderBuilder` 配置 `has_headers`（仅 Dict 模式开）与 `delimiter`；
- [csv.rs:55-69](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/csv.rs#L55-L69) `line_offset` 处理表头占行：Dict 模式从第 2 行起报错，Array 模式从第 1 行起（注释解释了为何不直接用库报的行号——与 `has_headers=false` 不兼容，见链接里的 rust-csv issue）；
- [csv.rs:78-88](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/csv.rs#L78-L88) 两种行表示：Dict 用 `headers.iter().zip(&row)` 组字典，Array 把字段直接收成数组。

`RowType` 用类型（`Type`）做开关而非字符串：

[src/loading/csv.rs:113-135](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/csv.rs#L113-L135) —— 用户写 `row_type: dictionary` 时，传入的是类型值；`cast!` 比较 `Type::of::<Dict>()` / `Type::of::<Array>()` 决定变体。这是 Typst 里「用类型当配置开关」的典型用法。

[src/loading/csv.rs:94-111](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/csv.rs#L94-L111) —— `Delimiter` 默认逗号，`cast!` 强制必须是 ASCII 字符。[format_csv_error](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/csv.rs#L138-L157) 把 UTF-8 错误、字段数不等等分类成友好消息。

**`xml()`**：

[src/loading/xml.rs:57-72](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/xml.rs#L57-L72) —— 先 `as_str` 再用 `roxmltree::Document::parse_with_options`（开了 `allow_dtd`），错误经 `format_xml_error` → `format_xml_like_error` 归位。

递归转换函数是 xml 的核心：

[src/loading/xml.rs:75-98](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/xml.rs#L75-L98) —— `convert_xml` 三种情况：

1. 文本节点 → 字符串；
2. 根节点 → `Array`（顶层子节点数组，所以 `xml()` 返回值外面那层是数组，文档示例用 `data.first().children` 取根）；
3. 元素节点 → `dict!{ namespace, tag, attrs, children }`，`attrs` 是属性名→字符串的字典，`children` 递归。

#### 4.4.4 代码实践

**实践目标**：对比 `csv` 的两种 `row_type`，理解同一份数据如何被组织成不同结构。

**操作步骤**（源码阅读 + 待本地验证）：

1. 阅读文档示例 [src/loading/csv.rs:9-25](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/csv.rs#L9-L25)，它用默认 `row_type: array` 把 CSV 喂进 `table`。
2. 准备一份 `people.csv`：
   ```csv
   name,age
   Ada,36
   Alan,41
   ```
3. 写两段对照（待本地验证）：
   ```typst
   #csv("people.csv")                       // 预期：(("name","age"),("Ada","36"),("Alan","41"))
   #csv("people.csv", row_type: dictionary) // 预期：((name:"name",age:"age"),(name:"Ada",age:"36"),...)
   ```
4. 阅读 [csv.rs:59-69](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/csv.rs#L59-L69) 解释为什么 dictionary 模式下第一行不会出现在数据里（它被当表头消费了）。

**需要观察的现象 / 预期结果**：

- `array` 模式返回「数组的数组」，所有行（含表头行）都是字符串数组；
- `dictionary` 模式返回「字典数组」，第一行被读为键、其余行被 zip 成字典，故数据行数比原文件少一行。

#### 4.4.5 小练习与答案

**练习 1**：`xml()` 返回的最外层为什么是数组而不是直接一个代表根元素的字典？

> **答案**：因为 XML 文档根之下可能有多个顶层节点（文本、注释、处理指令等），`convert_xml` 对 `is_root()` 节点返回所有 `children` 组成的 `Array`（[xml.rs:81-83](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/xml.rs#L81-L83)）。用户通常用 `.first()` 取到那个根元素再访问 `.children`。

**练习 2**：`csv` 为什么不也走 serde 桥？

> **答案**：CSV 是「列位置驱动」的二维表，没有稳定的类型信息（每个单元格都是字符串），与 serde 的「键值/类型化」模型不契合；手写循环可以灵活支持「数组行 vs 字典行」两种表示，并能精确处理表头行号。

---

## 5. 综合实践

把本讲四个模块串起来，完成一项「数据全景」小任务。

**任务**：假设你要排版一份小型报表，数据分别存为 `meta.json`、`scores.csv`、`note.xml` 三种格式。请：

1. **统一抽象层（4.1）**：用一句话写出「无论哪种格式，第一步都是 `source.load(engine.world)` 拿到 `Loaded`」，并指出这一步发生在 [src/loading/mod.rs:82-112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/mod.rs#L82-L112)。
2. **文本读取（4.2）**：用 `read("note.xml")` 读出原始字符串，再用 `read("logo.png", encoding: none)` 读二进制，对比两者返回类型。
3. **结构化解析（4.3）**：`#let meta = json("meta.json")`，验证 `meta.title` 是 `str`、`meta.version` 是 `float`/`int`，并解释这条映射发生在 [src/foundations/value.rs:380-493](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/value.rs#L380-L493) 的哪几个 visit 方法。再用 `json.encode(meta)` 验证往返。
4. **表格（4.4）**：分别用 `array` 与 `dictionary` 两种 `row_type` 读 `scores.csv`，画出两种返回结构。
5. **错误归位**：故意把 `meta.json` 改成非法 JSON，运行后观察错误信息——它应当指向 `meta.json` 内部出错的行列（因为 `LoadSource::Path`），而不是你的 `.typ` 调用处。解释这条 span 来自 [src/diag.rs:848-886](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L848-L886)。

> 实际编译结果「待本地验证」——本任务重在让你把「取字节 → 解析 → 归位」三段式与每个格式对应起来，而非替你跑 typst。

## 6. 本讲小结

- `loading` 模块用 `DataSource`（`Path` | `Bytes`）把「路径」与「现成字节」两种输入统一；`Load` trait 负责把它们都变成 `Loaded { source, data }`，分叉点在 [mod.rs:82-112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/loading/mod.rs#L82-L112)。
- `Loaded` 携带的 `LoadSource` 不是给数据用的，而是给**错误归位**用的：解析失败时决定 span 落到外部文件还是调用处。
- 七个函数遵循统一三段式「`source.load` → 解析 → `.within(&loaded)`」，`read` 只读不解析、`csv`/`xml` 手写转换、`json`/`yaml`/`toml`/`cbor` 走 serde。
- json/yaml/toml/cbor 能直接产出 Typst `Value`，靠的是 `Value` 自身实现的 serde `Deserialize`（`ValueVisitor`，[value.rs:368-493](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/value.rs#L368-L493)）——一座四格式共享的转换桥。
- `LoadError` 故意不带 span/FileId 以免污染 comemo 缓存，span 由 `LoadedWithin::within` 在最后现场补上（[diag.rs:761-844](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L761-L844)）。
- 贯穿主线依旧成立：本 crate 只做「定义与归一化」，真正的文件 IO 通过 `Engine.world.file(...)` 回调到 `World` 实现者（u5-l1）。

## 7. 下一步学习建议

- **`OneOrMultiple<DataSource>` 的真实消费者**：本讲只点到为止，建议接着读 [src/model/bibliography.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/bibliography.rs) 与 [src/text/raw.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/raw.rs)，看「多源加载」如何服务于 `#bibliography` 与 raw 语法高亮。
- **路径解析与项目沙箱**：`PathOrStr::resolve` 与 `resolve_if_some` 是 Typst 文件系统沙箱的入口，配合 `--root` 参数；可与 [src/foundations/path.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/path.rs) 的 `PathError::Escapes` 一起读。
- **诊断两段式**：若 `LoadError`/`LoadedWithin` 还没看够，u5-l3 已系统讲过 `bail!`/`At`/`Hint`；本讲是它的一个真实用例。
- **下一讲 u11-l2（sys 模块、WASM plugin 与符号系统）**：将离开「数据加载」进入「系统能力」，讲解 `sys.version`/`sys.inputs`、`plugin()` 如何加载 WebAssembly 模块并导出函数，以及 `codex` 符号表如何变成 `math.sym` 命名空间。
