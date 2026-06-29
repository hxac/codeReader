# zenoh-ext 序列化：跨语言通用序列化

## 1. 本讲目标

学完本讲，你应该能够：

- 用 `z_serialize` / `z_deserialize` 把基本类型、集合、元组编解码为 `ZBytes`。
- 理解 `Serialize` / `Deserialize` 两个 trait 的设计，并为自定义结构体**手写**它们的实现。
- 说清 `VarInt`（LEB128 变长整数）在序列化格式里扮演的角色，以及为什么这套格式对跨语言 binding 友好。

承接《u5-l1 ZBytes 与 Encoding》：上一讲我们认识到 `ZBytes` 只是「零拷贝字节容器」，Zenoh 协议本身并不关心里面装的是 JSON、protobuf 还是裸字符串。本讲回答下一个自然的问题——如果我不想自己手写 `serde_json` / protobuf，有没有一种 Zenoh 原生、轻量、跨语言通用的序列化方式？答案就是 `zenoh-ext` 提供的序列化格式。

## 2. 前置知识

- **ZBytes**：Zenoh 的字节负载容器（见《u5-l1》）。本讲所有序列化产物最终都是 `ZBytes`，可以直接传给 `publisher.put(...)`。
- **序列化（serialization）**：把内存里的结构化数据（结构体、数组、字符串……）转成一段可以网络传输的字节流；反序列化（deserialization）则是逆过程。
- **字节序（endianness）**：多字节整数在内存里的排放顺序。本讲格式统一用**小端序（little-endian）**，所以同一个数值在任何机器上编码出的字节都一样。
- **LEB128**：一种变长整数编码（Little Endian Base 128），用「每 7 位一组、最高位作继续标志」的方式压缩整数，小数用 1 字节、大数才多用几字节。下文会详述。
- **serde**：Rust 生态最流行的序列化框架。请注意，本讲讲的 `Serialize` / `Deserialize` **不是** serde 的那两个 trait，而是 `zenoh-ext` 自定义的、互不相同的两套（见 4.2 节的提醒）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [zenoh-ext/src/serialization.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs) | 序列化格式的全部实现：两个 trait、`z_serialize`/`z_deserialize` 入口、`ZSerializer`/`ZDeserializer`、各基本类型的实现、`VarInt`。本讲主角。 |
| [zenoh-ext/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/lib.rs) | crate 门面，决定哪些符号对外可见、受哪些 feature 门控。 |
| [examples/examples/z_bytes.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_bytes.rs) | 官方示例，集中演示了 `z_serialize`/`z_deserialize` 对数值、`Vec`、`HashMap`、元组、数组的用法。 |

> 提示：任务规格里列出的 `examples/examples/z_formats.rs` 实际讲的是 **key expression 的格式化/解析**（`kedefine!`/`keformat!`），与序列化无关，本讲不展开，留到 key expression 相关讲义。真正演示序列化的示例是 `z_bytes.rs`。

## 4. 核心概念与源码讲解

本讲三个最小模块：**z_serialize / z_deserialize（对外入口）**、**Serialize / Deserialize trait（实现机制）**、**VarInt（LEB128 变长整数）**。最后用一个二进制布局的小节把三者串起来，并说明跨语言意义。

### 4.1 z_serialize / z_deserialize：对外入口

#### 4.1.1 概念说明

绝大多数情况下，你只需要两个函数：

- `z_serialize<T: Serialize + ?Sized>(t: &T) -> ZBytes`：把任意实现了 `Serialize` 的值编码成 `ZBytes`，**不获取所有权**（只借用）。
- `z_deserialize<T: Deserialize>(zbytes: &ZBytes) -> Result<T, ZDeserializeError>`：从 `ZBytes` 里解码出指定类型 `T`，目标类型靠**类型参数**给出；若字节无法解码成该类型则返回错误。

它的典型用法是：发布端 `let payload = z_serialize(&data); publisher.put(payload).await?;`，订阅端从 `Sample` 拿到 `payload: ZBytes` 后 `let data: MyType = z_deserialize(&payload)?;`。

#### 4.1.2 核心流程

这两个函数本身只是「序列化器/反序列化器」的薄包装：

- `z_serialize`：`new` 一个 `ZSerializer` → 调 `serialize` → `finish` 得到 `ZBytes`。
- `z_deserialize`：`new` 一个 `ZDeserializer` → 调 `T::deserialize` → **校验是否读完**（`deserializer.done()`），若还剩字节也算失败。

注意 `z_deserialize` 末尾有一个「必须读完」的校验：如果输入字节比类型 `T` 需要的多，会返回 `Err`。这是为了防止「解码了一个 `i32` 却喂了 8 字节」这类静默错误。

#### 4.1.3 源码精读

入口函数实现非常短，正好看清「序列化器三步走」：

[zenoh-ext/src/serialization.rs:L132-L136](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs#L132-L136) —— `z_serialize`：建序列化器、编码、收尾。

```rust
pub fn z_serialize<T: Serialize + ?Sized>(t: &T) -> ZBytes {
    let mut serializer = ZSerializer::new();
    serializer.serialize(t);
    serializer.finish()
}
```

[zenoh-ext/src/serialization.rs:L152-L159](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs#L152-L159) —— `z_deserialize`：建反序列化器、解码，并校验「读完」。

```rust
pub fn z_deserialize<T: Deserialize>(zbytes: &ZBytes) -> Result<T, ZDeserializeError> {
    let mut deserializer = ZDeserializer::new(zbytes);
    let t = T::deserialize(&mut deserializer)?;
    if !deserializer.done() {
        return Err(ZDeserializeError);
    }
    Ok(t)
}
```

官方示例 `z_bytes.rs` 集中演示了对各种类型的往返（round-trip），可以直接照搬：

[examples/examples/z_bytes.rs:L73-L101](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_bytes.rs#L73-L101) —— 对 `u32`、`Vec<f32>`、`HashMap<u32, String>`、`(f64, String)` 元组的序列化/反序列化。

```rust
use zenoh_ext::{z_deserialize, z_serialize};
let input = 1234_u32;
let payload = z_serialize(&input);
let output: u32 = z_deserialize(&payload).unwrap();
assert_eq!(input, output);
```

#### 4.1.4 代码实践

**目标**：验证基本类型与集合的往返。

**步骤**：

1. 进入仓库根目录，编译运行 `z_bytes` 示例（该示例默认可用，无需特殊 feature）：

   ```bash
   cargo run --example z_bytes
   ```
2. 阅读示例 `z_bytes.rs` 第 73–114 行，观察 `u32`、`Vec<f32>`、`HashMap<u32,String>`、`(f64,String)`、`[f32;3]` 的写法。
3. 自己在示例里加一段：序列化一个 `Vec<i32>` 再反序列化回来，断言相等。

**需要观察的现象**：示例本身没有 `println!`（全是 `assert_eq!`），所以**正常运行没有任何输出**就代表全部断言通过；如果断言失败，`unwrap()` 会 panic 并打印错误。

**预期结果**：程序静默退出（exit code 0），说明所有类型的往返都正确。

> 待本地验证：不同 payload 大小下，`z_serialize` 出的 `ZBytes` 的字节数（可在断言后加一行 `println!("len={}", payload.len());` 观察）。

#### 4.1.5 小练习与答案

- **练习 1**：把 `z_serialize(&1234_u32)` 的结果，用 `z_deserialize::<u64>` 去解码，会发生什么？为什么？
  - **答案**：返回 `Err(ZDeserializeError)`。因为 `u32` 序列化是定长 4 字节，而 `u64` 反序列化需要 8 字节，`read_exact` 读不够就报错。
- **练习 2**：为什么 `z_deserialize` 最后要检查 `deserializer.done()`？去掉这个检查会有什么风险？
  - **答案**：防止「字节有剩余」被静默忽略。例如把一段 10 字节的数据当 `(i32, i32)`（共 8 字节）解码，不加检查就会成功并丢弃 2 字节，掩盖数据不匹配的 bug。

---

### 4.2 Serialize / Deserialize：trait 与序列化器

#### 4.2.1 概念说明

`Serialize` 和 `Deserialize` 是 `zenoh-ext` 自定义的两个 trait，定义了「如何把一个类型编进 / 读出 Zenoh 序列化格式」。它们的形态故意做得很简单：

- `Serialize::serialize(&self, serializer: &mut ZSerializer)`：把 `self` 追加写进序列化器。
- `Deserialize::deserialize(deserializer: &mut ZDeserializer) -> Result<Self, ZDeserializeError>`：从反序列化器读出一个 `Self`（注意 `Deserialize` 带 `Self: Sized` 约束）。

库里已经为一大批类型实现了它们：所有数值类型（`i8..i128`、`u8..u128`、`f32`/`f64`）、`bool`、`str`/`String`、切片/`Vec`/数组/`Box<[T]>`、`HashMap`/`HashSet`/`BTreeMap`/`BTreeSet`、`Cow`、`ZBytes`，以及最长到 16 元的**元组**。

> ⚠️ 重要提醒（避免踩坑）：这两个 trait **不是** serde 的 `Serialize`/`Deserialize`。仓库里 [zenoh-ext/src/group.rs:L45](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/group.rs#L45) 上写的 `#[derive(Serialize, Deserialize)]` 用的是 `use serde::{Deserialize, Serialize};`（见 [group.rs:L25](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/group.rs#L25)），那是 serde，配合 bincode 用，跟本讲的格式**完全不同**，两套互不兼容。

另一个关键事实：**本格式没有派生宏（derive macro）**。`zenoh-macros` 这个 crate（路径 `commons/zenoh-macros/src/lib.rs`）只提供 `GenericRuntimeParam` 和 `RegisterParam` 两个派生，没有任何针对序列化的派生。因此对自定义结构体，你必须**手写** `Serialize`/`Deserialize` 的实现——好在非常机械，就是「按字段顺序依次序列化 / 按相同顺序依次反序列化」（见 4.2.4 实践）。

#### 4.2.2 核心流程

整个机制是一个「序列化器游标」模型：

- `ZSerializer` 内部包了一个 `ZBytesWriter`（即《u5-l1》讲过的 `ZBytes` writer）。每调用一次 `serialize`，就把对应字段的字节**追加**到尾部；多次调用等价于序列化一个元组。
- `ZDeserializer` 内部包了一个 `ZBytesReader`，维护一个**读游标**。每调用一次 `deserialize`，就从游标当前位置往后读对应字节，并把游标前移。
- 因此「先写什么，就要先读什么」——字段顺序必须严格一致；元组的实现就是按位置依次读写。

数值类型走定长小端序；变长序列（`Vec`/`String`/`HashMap` 等）先写一个 `VarInt` 长度，再逐个元素写。

#### 4.2.3 源码精读

两个 trait 的定义——注意 `Deserialize` 带 `Self: Sized`，并提供了 `serialize_n`/`deserialize_n` 这类批量优化的默认方法：

[zenoh-ext/src/serialization.rs:L47-L59](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs#L47-L59) —— `Serialize` trait。

```rust
pub trait Serialize {
    fn serialize(&self, serializer: &mut ZSerializer);
    #[doc(hidden)]
    fn serialize_n(slice: &[Self], serializer: &mut ZSerializer) where Self: Sized {
        default_serialize_n(slice, serializer);
    }
}
```

[zenoh-ext/src/serialization.rs:L92-L111](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs#L92-L111) —— `Deserialize` trait（`Sized` + 默认批量方法）。

`ZSerializer` / `ZDeserializer` 是两个很薄的包装器，分别持有 `ZBytesWriter` 和 `ZBytesReader`：

[zenoh-ext/src/serialization.rs:L178-L214](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs#L178-L214) —— `ZSerializer`：`new` / `serialize` / `serialize_iter` / `finish`。

[zenoh-ext/src/serialization.rs:L245-L279](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs#L245-L279) —— `ZDeserializer`：`new` / `done` / `deserialize` / `deserialize_iter`。

数值类型的实现由宏批量生成，统一用**定长小端序**：

[zenoh-ext/src/serialization.rs:L327-L374](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs#L327-L374) —— `impl_num!` 宏：`to_le_bytes` 写、`from_le_bytes` 读；在小端机器上对数值切片还有一次性整块写的优化（`align_to`）。

```rust
impl Serialize for $ty {
    fn serialize(&self, serializer: &mut ZSerializer) {
        serializer.0.write_all(&(*self).to_le_bytes()).unwrap();
    }
}
```

变长序列统一走「先写 `VarInt` 长度，再写元素」，以 `Vec` 为例：

[zenoh-ext/src/serialization.rs:L391-L407](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs#L391-L407) —— `serialize_slice` / `deserialize_slice`：`VarInt(slice.len())` 记长度，再批量读写元素。

[zenoh-ext/src/serialization.rs:L500-L519](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs#L500-L519) —— `String` 复用 `[u8]` 的序列化（先 VarInt 长度再 UTF-8 字节），反序列化时用 `String::from_utf8` 校验合法性。

元组由宏按位置依次序列化（最长 16 元），这正是「多次 serialize 等价于序列化元组」的来源：

[zenoh-ext/src/serialization.rs:L521-L564](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs#L521-L564) —— `impl_tuple!` 宏。

最后看门面 [zenoh-ext/src/lib.rs:L61-L66](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/lib.rs#L61-L66)：核心 API（`z_serialize`/`z_deserialize`/两个 trait/`ZSerializer`/`ZDeserializer`/`ZDeserializeError`/`ZReadIter`）**无条件导出**，而 `VarInt` 被 `#[cfg(feature = "internal")]` 单独门控——也就是说基础序列化无需任何 feature，但要用 `VarInt` 得开 `internal`。

#### 4.2.4 代码实践

**目标**：为自定义结构体手写 `Serialize`/`Deserialize`，验证往返。

**步骤**：

1. 在一个依赖 `zenoh` 与 `zenoh-ext` 的小程序里定义结构体并手写两个 trait（注意字段顺序必须读写一致）：

   ```rust
   // 示例代码：手动实现 zenoh-ext 的 Serialize/Deserialize（本格式无 derive 宏）
   use zenoh_ext::{Deserialize, Serialize, ZDeserializeError, ZDeserializer, ZSerializer};

   #[derive(Debug, PartialEq)]
   struct SensorReading {
       name: String,
       id: u32,
       values: Vec<f64>,
   }

   impl Serialize for SensorReading {
       fn serialize(&self, s: &mut ZSerializer) {
           self.name.serialize(s);      // 字段 1
           self.id.serialize(s);        // 字段 2
           self.values.serialize(s);    // 字段 3
       }
   }

   impl Deserialize for SensorReading {
       fn deserialize(d: &mut ZDeserializer) -> Result<Self, ZDeserializeError> {
           Ok(Self {
               name: String::deserialize(d)?,       // 按相同顺序读回
               id: u32::deserialize(d)?,
               values: Vec::<f64>::deserialize(d)?,
           })
       }
   }
   ```
2. 用 `z_serialize` / `z_deserialize` 验证往返：

   ```rust
   let r = SensorReading { name: "temp".into(), id: 7, values: vec![36.5, 36.6] };
   let payload = zenoh_ext::z_serialize(&r);
   let back: SensorReading = zenoh_ext::z_deserialize(&payload).unwrap();
   assert_eq!(r, back);
   ```

**需要观察的现象**：断言通过；若把反序列化里字段顺序调换（例如先读 `id` 再读 `name`），会因字节对不上而 panic 或得到乱码。

**预期结果**：`assert_eq!(r, back)` 通过。

> 待本地验证：把某个字段改成与发布端不同的类型（如把 `id` 反序列化成 `u64`），确认会得到 `ZDeserializeError`。

#### 4.2.5 小练习与答案

- **练习 1**：如果两个结构体 `A` 和 `B` 字段类型完全一样但顺序不同，能用 `z_serialize(&a)` 后 `z_deserialize::<B>` 成功吗？
  - **答案**：通常不能。即便字段类型相同，顺序不同会导致字节错位（尤其当含变长序列时，`VarInt` 长度会被当成数据读）。这正是「按字段顺序严格读写」的代价。
- **练习 2**：为什么不直接给所有结构体 `#[derive(Serialize, Deserialize)]`？
  - **答案**：因为本格式**没有**对应的派生宏（`zenoh-macros` 未提供）。要派生，得用 serde 的同名 trait（那是另一套格式）。所以本格式下自定义类型只能手写两个 trait。

---

### 4.3 VarInt：LEB128 变长整数

#### 4.3.1 概念说明

`VarInt` 是一个对整数的「变长编码」包装器，底层用 **LEB128**（Little Endian Base 128）算法。它解决一个问题：序列里元素的个数（长度）大多很小（比如 3 个浮点数），但偶尔也会很大（几十万个）。如果长度固定用 8 字节 `usize`，每个序列都白费很多字节；LEB128 让小数只占 1 字节、大数才多占几字节。

`VarInt` 的定义就是一个 `#[repr(transparent)]` 的新类型包装：

[zenoh-ext/src/serialization.rs:L575-L577](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs#L575-L577) —— `pub struct VarInt<T>(pub T);`，内部值就是被包装的整数。

#### 4.3.2 核心流程

LEB128（无符号）的编码规则：把整数按「每 7 位一组」切成多段，每段拼进一个字节的低 7 位；若后面还有段，就把该字节的**最高位（bit 7）置 1** 表示「继续」，否则置 0 表示「结束」。

例如：

- 长度 `4` → 二进制 `0000100`，只有 1 段 → 编码为单字节 `0x04`。
- 长度 `300` → 二进制 `100101100`，拆成两段：低 7 位 `0101100`（0x2C）、高 2 位 `10`（0x02）；第一字节带继续标志 → `0x2C | 0x80 = 0xAC`，第二字节无后续 → `0x02`。即 `[0xAC, 0x02]`，共 2 字节。

解码即逆过程：边读边累加，遇到最高位为 0 的字节停止。库里直接复用 [`leb128`](https://crates.io/crates/leb128) crate 完成。

#### 4.3.3 源码精读

[zenoh-ext/src/serialization.rs:L566-L588](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs#L566-L588) —— `VarInt<usize>` 的 `Serialize`/`Deserialize`，内部委托给 `leb128::write::unsigned` / `leb128::read::unsigned`：

```rust
impl Serialize for VarInt<usize> {
    fn serialize(&self, serializer: &mut ZSerializer) {
        leb128::write::unsigned(&mut serializer.0, self.0 as u64).unwrap();
    }
}
```

注意 doc 注释里两点重要事实（[L566-L577](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs#L566-L577)）：

1. 目前只实现了 `VarInt<usize>` 一种（不是所有整数都能当 VarInt 用）。
2. 它**主要供库内部使用**——序列化各种序列（切片、`Vec`、`String`、`HashMap` 等）的长度时都用它；用户也可以用它把整数以更紧凑的方式编码。
3. 通过门面 [lib.rs:L61-L62](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/lib.rs#L61-L62) 可知，`VarInt` 受 `internal` feature 门控，普通用户默认拿不到。

#### 4.3.4 代码实践（源码阅读型）

**目标**：从单元测试里直观看到 LEB128 在字节流里的样子。

**步骤**：

1. 阅读 [zenoh-ext/src/serialization.rs:L680-L700](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs#L680-L700) 的 `binary_format` 测试，它断言了若干值的精确字节输出。
2. 重点关注这一条：字符串 `"test"` 编码为 `[4, 116, 101, 115, 116]`——开头那个 `4` 正是 `VarInt(4)`（长度 4 用 LEB128 编码就是单字节 `0x04`），后面 4 个字节是 `'t','e','s','t'` 的 ASCII。
3. 运行这个测试：

   ```bash
   cargo test -p zenoh-ext --lib serialization::tests::binary_format
   ```

**需要观察的现象**：测试通过；可在测试里临时加 `println!("{:?}", payload.to_bytes())` 观察真实字节（需 `-- --nocapture`）。

**预期结果**：`test serialization::tests::binary_format ... ok`。

> 待本地验证：构造一个长度 ≥ 128 的 `Vec<u8>`（如 `vec![0u8; 300]`），打印 `z_serialize` 的前两字节，确认长度 `300` 被编码成 `[0xAC, 0x02]` 两字节（而非 8 字节）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么序列化 `Vec` 时长度用 `VarInt` 而不是固定 8 字节 `usize`？
  - **答案**：绝大多数序列很短，LEB128 能用 1 字节表示 0–127，显著省带宽；只有超长序列才多用字节，且仍能正确表达任意长度。
- **练习 2**：用户代码里能直接 `z_serialize(&VarInt(42usize))` 吗？
  - **答案**：要分情况。`VarInt` 的 `Serialize` 实现是公开的，但 `VarInt` 类型本身受 `internal` feature 门控（见门面 lib.rs）。所以必须开启 `zenoh-ext` 的 `internal` feature 才能在用户代码里直接命名 `VarInt`。

---

### 4.4 二进制布局与跨语言意义（综合）

把上面三个模块合起来看，一条数据的字节布局就是「字段依次拼接」：定长数值用定长小端序，变长序列用 `VarInt 长度 + 元素`。以测试里的元组 `(u16, f32, &str) = (500, 1234.0, "test")` 为例（断言见 [serialization.rs:L688-L689](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs#L688-L689)）：

| 字段 | 编码方式 | 字节 |
| --- | --- | --- |
| `u16 500` | 小端定长 2 字节 | `244, 1` |
| `f32 1234.0` | 小端定长 4 字节（IEEE754） | `0, 64, 154, 68` |
| `&str "test"` | `VarInt(4)` + UTF-8 字节 | `4, 116, 101, 115, 116` |

拼起来正好是 `[244, 1, 0, 64, 154, 68, 4, 116, 101, 115, 116]`，与测试断言完全一致。

**为什么这套格式对跨语言 binding 友好？**

1. **规范独立于语言**：它有独立的 RFC（[Serialization.md](https://github.com/eclipse-zenoh/roadmap/blob/main/rfcs/ALL/Serialization.md)，源码 doc 注释里给出的链接），不绑定 Rust 类型系统。Python/C/Java 各 binding 只需按同一份规范实现编解码，就能与 Rust 端互通。
2. **平台无关的数值编码**：统一小端序 + IEEE754 浮点，任意 CPU 架构编出的字节都一样。
3. **没有 Rust 特有的边角**：相比 `serde + bincode` 这类 Rust 中心化方案（可能依赖 Rust 的枚举表示、变体顺序等），本格式刻意保持极简：只有「定长小端数值 + LEB128 长度前缀的序列 + 元组拼接」，便于其它语言逐字实现。
4. **省带宽**：LEB128 让常见的小长度只占 1 字节，适合物联网/边缘场景。

一句话：`zenoh-ext` 的序列化是 Zenoh 的「最小公约数」二进制格式——简单、紧凑、跨语言，代价是自定义结构体需要手写 trait（无派生宏）。

## 5. 综合实践

把本讲三件事（`z_serialize`/`z_deserialize`、手写 trait、结合 Pub/Sub）串成一个端到端任务。

**任务**：实现一个温度上报的发布端与订阅端，负载用自定义结构体经 `zenoh-ext` 序列化。

**发布端**（`z_pub_serde.rs`，示例代码）：

```rust
#[derive(Debug, PartialEq)]
struct Reading { name: String, id: u32, values: Vec<f64> }
// 为 Reading 手写 Serialize / Deserialize（同 4.2.4，略）

#[tokio::main]
async fn main() {
    let session = zenoh::open(zenoh::Config::default()).await.unwrap();
    let publisher = session.declare_publisher("sensor/temp").await.unwrap();
    for i in 0..5 {
        let r = Reading { name: "temp".into(), id: i, values: vec![36.0 + i as f64 * 0.1] };
        publisher.put(zenoh_ext::z_serialize(&r)).await.unwrap();
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
    }
}
```

**订阅端**（`z_sub_serde.rs`，示例代码）：

```rust
#[tokio::main]
async fn main() {
    let session = zenoh::open(zenoh::Config::default()).await.unwrap();
    let subscriber = session.declare_subscriber("sensor/temp").await.unwrap();
    while let Ok(sample) = subscriber.recv_async().await {
        let r: Reading = zenoh_ext::z_deserialize(&sample.payload().clone())
            .expect("deserialize failed");
        println!("{:?}: {:?}", sample.key_expr(), r);
    }
}
```

**验证要点**：

1. 两端能匹配（key expression `sensor/temp` 相交），订阅端打印出 5 条 `Reading`。
2. 若把订阅端 `Reading` 的字段顺序写错（如先 `id` 后 `name`），反序列化会失败——体会「字段顺序必须严格一致」。
3. 思考：为什么这里用 `z_serialize` 而不是 `serde_json`？答：跨语言互通 + 省带宽。可尝试同一结构体另写一个 JSON 版本对比 `payload.len()`。

> 待本地验证：完整可编译需要把 `Reading` 的两个 trait 实现补全，并在 `Cargo.toml` 里依赖 `zenoh`（`default-features=false` 后按需开 feature）与 `zenoh-ext`。若不想引入 tokio，可参考 `examples/examples/z_pub.rs` 的 runtime 用法。

## 6. 本讲小结

- `z_serialize` / `z_deserialize` 是序列化的两个对外入口，产物/输入都是 `ZBytes`，可直接用作 `put` 的负载。
- `Serialize` / `Deserialize` 是 `zenoh-ext` **自定义**的 trait，**不是** serde 的同名 trait，两者不兼容；本格式**没有派生宏**，自定义结构体需手写实现（按字段顺序读写）。
- 数值走定长小端序，变长序列（`Vec`/`String`/`HashMap` 等）走「`VarInt` 长度前缀 + 元素」，元组/连续 `serialize` 就是字段依次拼接。
- `VarInt` 基于 LEB128，小整数省字节；它主要供库内部记录序列长度，类型本身受 `internal` feature 门控。
- 这套格式独立于语言、平台无关、紧凑，是 Zenoh 跨语言 binding 互通的基础；代价是无派生宏、字段顺序敏感。

## 7. 下一步学习建议

- **横向对比**：阅读 `examples/examples/z_bytes.rs` 里 JSON、protobuf 与 `zenoh-ext` 三种序列化并列的写法，理解「`ZBytes` 只是容器，编码方式自选」的边界（呼应《u5-l1》）。
- **进阶到高级 Pub/Sub**：本讲的手写结构体序列化是后续《u12-l2 高级 pub/sub（zenoh-ext）》里 `AdvancedPublisher`/`PublicationCache` 的基础，建议接着学。
- **深入格式规范**：若想为其它语言写 binding，直接读 [Serialization RFC](https://github.com/eclipse-zenoh/roadmap/blob/main/rfcs/ALL/Serialization.md) 与 [serialization.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh-ext/src/serialization.rs) 里的 `binary_format` 测试，二者是权威的字节级参考。
