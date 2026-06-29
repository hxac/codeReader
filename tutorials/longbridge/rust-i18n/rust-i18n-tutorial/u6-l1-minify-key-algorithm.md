# minify_key 短键算法原理

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 rust-i18n 为什么需要「短键（minify_key）」，以及它解决了什么问题。
- 看懂 `minify_key` 函数的四参数语义：`value`、`len`、`prefix`、`threshold`，以及「何时返回原文、何时返回短键」的判定逻辑。
- 理解短键的生成链路：`SipHasher13` 计算 128 位哈希 → `base62` 编码成可读短串 → 截断并加前缀。
- 说出四个 `DEFAULT_MINIFY_KEY_*` 默认常量的取值与含义，并解释为什么 `DEFAULT_MINIFY_KEY_LEN = 24` 而实际生成的短键往往是 22 位。
- 能在本地跑通单元测试，亲手验证 `"Hello, world!"` 的短键值，并解释 `threshold=128` 时为何原样返回。

本讲是「minify_key 短键机制」单元的第一篇，只讲**算法本身**（位于 support crate 的纯函数）；至于这套算法如何被 `_minify_key!` 宏在编译期调用、如何被 `_tr!` 透传，留待 u6-l2、u6-l3 承接。

## 2. 前置知识

在进入算法前，先建立两个直觉。本讲假设你已学完 u3-l3（变量插值与格式化），知道 `t!` 内部最终都要拿一个「字符串 key」去 `_RUST_I18N_BACKEND` 里查表。

### 2.1 「文案即 key」与它的代价

rust-i18n 有一种很自然的用法：直接把**人类可读的句子**当作翻译键，例如：

```rust
t!("Hello, %{name}!", name = "World");
```

这种「文案即 key」的写法对开发者极其友好——不需要再为每句话起一个 `messages.hello` 之类的短名，读代码时一眼就懂。但代价是：这些长句子会作为 key 被反复存储——在 `SimpleBackend` 的 `HashMap<key, value>` 里、在编译期生成的代码里、在每一处 `t!` 调用点。一个几十上百字节的长句重复成百上千次，会显著增加二进制体积与内存占用，也让 `HashMap` 的字符串哈希、比较变慢。

**短键（minify_key）**就是为这个问题设计的：把长文案压缩成一个固定长度的短字符串当 key，译文照旧，但 key 本身变得很小。

### 2.2 什么是哈希与 base62

- **哈希（hash）**：把任意长度的输入（这里是句子的 UTF-8 字节）通过一个确定性函数映射成一段**定长**的「指纹」。相同输入永远得到相同输出，不同输入（在概率意义上）几乎不会撞到同一个输出。本讲用的是 `SipHasher13`，输出 128 位（16 字节）。
- **base62 编码**：类似我们熟悉的十进制（10 个符号 `0-9`）、十六进制（16 个符号 `0-9A-F`），base62 用 **62 个符号**（`0-9`、`A-Z`、`a-z`）来表示一个数。进制越大，表示同一个数需要的「位数」越短。128 位的二进制数用 base62 表示只需约 22 个字符。

把这两步串起来：长句子 → 128 位哈希 → base62 短串。这就是短键的核心思想。

### 2.3 一个关键约束：确定性

短键必须**完全确定**：编译期宏生成的 key、提取器（`cargo i18n`）写到翻译文件里的 key、运行时 `t!` 查表用的 key，三者必须一字不差地相同，否则查不到译文。因此本讲用的哈希函数 `SipHasher13::new()` 使用**固定的默认密钥**，对同一字符串在任何机器、任何一次编译下都产生同一结果。这是「确定性哈希」而非「加密安全哈希」——它不为防篡改，只为稳定地生成 ID。

## 3. 本讲源码地图

本讲只涉及一个文件，它是短键算法的全部所在：

| 文件 | 作用 |
| --- | --- |
| [crates/support/src/minify_key.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs) | 定义四个默认常量、`hash128` 哈希函数、`minify_key` 短键生成函数、`MinifyKey` trait 及其对各种字符串类型的实现，并附带单元测试。 |

辅助理解的参考文件（不属于本讲算法本身，仅供印证）：

| 文件 | 作用 |
| --- | --- |
| [crates/macro/src/minify_key.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/minify_key.rs) | 编译期 `_minify_key!` 过程宏，它在编译期直接调用本讲的 `minify_key` 函数算出常量 key（详见 u6-l2）。 |
| [tests/i18n_minify_key.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/i18n_minify_key.rs) | 集成测试，配置 `minify_key` 系列参数并用 `tkv!` 验证生成的 `(key, msg)`，是本讲实践的依据。 |

## 4. 核心概念与源码讲解

本讲按「先看总览函数，再拆三个零件，最后讲默认常量」的顺序，拆成 4 个最小模块。

### 4.1 minify_key 函数总览与「需要短键」的动机

#### 4.1.1 概念说明

`minify_key` 是整个短键机制的入口函数。它做一件看似简单的事：

- 如果文案**足够短**（长度不超过 `threshold`），就**原样返回**——短文案本身已经够小，没必要再压缩，直接当 key 用更可读。
- 如果文案**足够长**，就把它压缩成一个 base62 短串，可选地加上前缀，作为新的 key。

注意它的返回类型是 `Cow<'r, str>`（沿用 u3-l3 / u8-l2 会反复出现的「写时复制」类型）：
- 返回原文时用 `Cow::Borrowed(value)`，**零分配**，直接借用入参；
- 返回短键时才 `format!` 出一个新 `String`（`Owned`）。

这呼应了短键的设计目标：**在命中短路的常见情况下尽量不分配内存**。

#### 4.1.2 核心流程

`minify_key(value, len, prefix, threshold)` 的执行流程可以用伪代码描述：

```
fn minify_key(value, len, prefix, threshold):
    if value.len() <= threshold:        # ① 短路：够短就直接返回原文
        return Borrowed(value)
    encoded = base62_encode(hash128(value))   # ② 哈希 + base62
    take = min(len, encoded.len())      # ③ 截断长度上限，不能越界
    return Owned(prefix + encoded[..take])    # ④ 加前缀
```

四个参数的含义：

| 参数 | 含义 | 典型取值 |
| --- | --- | --- |
| `value` | 要被压缩的原文案 | `"Hello, world!"` |
| `len` | 短键的**最大**长度（截断上限） | `24`（默认） |
| `prefix` | 短键前缀，用于命名空间隔离 | `""`、`"t_"`、`"mytr_"` |
| `threshold` | 启用短键的**最小文案长度阈值**，文案长度 ≤ 它则不压缩 | `127`（默认） |

> 名词辨析：`threshold` 是「**要不要**压缩」的开关阈值；`len` 是「**压缩后留多长**」的截断上限。两者完全独立。

#### 4.1.3 源码精读

函数本体非常短，关键就在前两行的短路判定和后三行的编码：

[crates/support/src/minify_key.rs:39-46](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs#L39-L46) —— `minify_key` 函数本体：先 `value.len() <= threshold` 判定是否短路返回原文；否则 `base62::encode(hash128(value))` 算出短串，用 `len.min(encoded.len())` 安全截断（避免 `len` 超过实际编码长度时切片越界 panic），最后 `format!` 拼上前缀。

这里有两个易被忽略的细节：

1. **比较的是字节长度 `value.len()`，不是字符数**。对 ASCII 句子两者相同，但含中文等多字节字符时，`len()` 是 UTF-8 字节数。这与「节省内存」的目标一致——决定是否压缩看的是它实际占用的字节数。
2. **`len.min(encoded.len())` 是必要的防越界保护**。`&encoded[..len]` 在 `len > encoded.len()` 时会 panic；`.min()` 把请求长度钳制到实际长度以内。下一节会看到，128 位哈希的 base62 编码最多 22 个字符，而默认 `len=24`，所以这个钳制在默认配置下**几乎总会生效**。

为了让各种字符串类型都能方便地调用，文件还定义了一个 `MinifyKey` trait 并对 `str`、`&str`、`String`、`&String`、`Cow<str>`、`&Cow<str>` 都做了实现：

[crates/support/src/minify_key.rs:49-59](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs#L49-L59) —— `MinifyKey` trait 与 `str` 的实现：trait 方法签名与自由函数一致，`str` 的实现直接委托给自由函数 `minify_key(self, ...)`，于是 `"Hello".minify_key(24, "", 0)` 这样的写法等价于 `minify_key("Hello", 24, "", 0)`。

> `String`/`Cow` 的实现稍有不同：它们在委托前会先自己做一次 `self.len() <= threshold` 的短路判定，以便在命中时返回**指向自身**的 `Cow::Borrowed`（而不是借用临时引用）。这是生命周期上的精细处理，对理解算法不影响，知道「各类型都能调」即可。

#### 4.1.4 代码实践

**实践目标**：亲手验证短路分支与压缩分支的差异。

**操作步骤**：

1. 打开 [crates/support/src/minify_key.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs) 的内联测试 `test_minify_key`。
2. 在仓库根目录运行 support crate 的单元测试：

   ```bash
   cargo test -p rust-i18n-support --lib minify_key
   ```

**需要观察的现象**：测试通过；控制台不报错。

**预期结果**：测试用例断言 `"Hello, world!"` 在 `threshold=0` 时被压缩成 `"1LokVzuiIrh1xByyZG4wjZ"`，而在 `threshold=128` 时原样返回 `"Hello, world!"`。两段断言都成立即说明两条分支工作正常。

> 若你的环境无法编译（例如缺少网络拉取依赖），此结果为「待本地验证」；断言值直接取自源码第 113-135 行，可先靠读代码确认。

#### 4.1.5 小练习与答案

**练习 1**：`minify_key("hi", 24, "", 5)` 会走哪条分支？返回什么？

**答案**：`"hi".len() == 2 <= 5`，命中短路分支，返回 `Cow::Borrowed("hi")`，即原文 `"hi"`，不分配内存。

**练习 2**：把 `len` 设成一个比编码结果还大的数（例如 `len=100`）会 panic 吗？为什么？

**答案**：不会。`len.min(encoded.len())` 会把截断长度钳制到实际编码长度以内，因此 `&encoded[..take]` 永远不会越界。这正是 `.min()` 存在的意义。

---

### 4.2 hash128：SipHasher13 与确定性 128 位哈希

#### 4.2.1 概念说明

`hash128` 是短键的第一步：把任意长度的字符串压成 **128 位（16 字节）**的定长指纹。它使用 [siphasher](https://github.com/djkoloski/siphasher) crate 提供的 `SipHasher13`。

**SipHash** 是一种为哈希表键设计的伪随机函数族（PRF），特点是快、抗碰撞、抗哈希洪泛攻击。名字里的数字代表轮次：`SipHasher13` 表示压缩阶段 1 轮、终结阶段 3 轮（对比常见的 SipHash-2-4 是 2 轮 + 4 轮）。轮次越少越快、安全性略低——对短键场景完全够用，因为我们追求的是**确定性 + 低碰撞**，而非加密安全。

为什么固定密钥？因为 `SipHasher13::new()` 使用默认密钥（全零），保证：

- 同一句子在任何机器、任何一次编译下哈希值都相同 → 编译期生成的 key、提取器写的 key、运行时查的 key 三者一致。
- 这与 Rust 标准库 `std::collections::HashMap` 默认的 **随机化 SipHash**（每次进程启动用随机种子防洪泛）恰恰相反——标准库要的是「不可预测」，而短键要的是「完全可复现」。

#### 4.2.2 核心流程

```
hash128(value):
    bytes = value.as_ref()                 # 取 UTF-8 字节
    h = TR_KEY_HASHER.hash(bytes)          # 用全局静态 SipHasher13 哈希
    return h.as_u128()                     # 取 128 位无符号整数
```

需要特别说明：`TR_KEY_HASHER` 是一个用 `LazyLock` 包裹的全局静态哈希器：

[crates/support/src/minify_key.rs:18](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs#L18) —— `static TR_KEY_HASHER: LazyLock<SipHasher13> = LazyLock::new(SipHasher13::new);`：首次访问时才创建一个默认密钥的 `SipHasher13`，此后所有 `hash128` 调用复用同一个实例。

> 这里复用同一实例是安全的，因为对 `SipHasher` 来说，每次 `.hash()` 调用都是从既定内部状态出发写入字节再读取结果，且 `hash128` 的调用之间没有跨调用的状态依赖（每个字符串独立成块）。`LazyLock` 既提供了「全局唯一」的便利，又避免了「运行时初始化顺序」问题。

128 位的空间有多大？一共 \(2^{128} \approx 3.4 \times 10^{38}\) 种可能。在生日悖论下，要出现两个不同句子哈希碰撞，期望需要约 \(\sqrt{2^{128}} = 2^{64} \approx 1.8 \times 10^{19}\) 个不同输入——对任何现实项目的翻译文案数量而言，碰撞概率都可视为零。

#### 4.2.3 源码精读

`hash128` 是一个对任意「能给出字节切片」的类型都适用的泛型函数：

[crates/support/src/minify_key.rs:21-23](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs#L21-L23) —— `hash128<T: AsRef<[u8]> + ?Sized>`：取入参的字节视图，喂给全局 `TR_KEY_HASHER`，调用 `.as_u128()` 把结果转成 `u128` 返回。`?Sized` 允许 `str`（动态大小类型）直接作为参数，所以 `hash128(&"Hello, world!")` 能直接编译。

注意它接收的是 `&T` 而非 `T`，再 `.as_ref()` 取字节——对 `str` 来说 `as_ref()` 得到的就是它的 UTF-8 字节序列。所以**哈希的是字符串的字节内容**，而不是它的某个「编号」。

#### 4.2.4 代码实践

**实践目标**：确认 `hash128` 的确定性。

**操作步骤**：

1. 在 `crates/support/src/minify_key.rs` 的 `#[cfg(test)] mod tests` 里，临时加一行断言（示例代码，非项目原有代码）：

   ```rust
   // 示例代码：验证同一输入两次哈希结果相同
   assert_eq!(hash128("Hello, world!"), hash128(&"Hello, world!".to_string()));
   ```

2. 运行 `cargo test -p rust-i18n-support --lib minify_key`。

**需要观察的现象**：断言通过。

**预期结果**：无论是 `&str` 还是 `String`，只要字节内容相同，`hash128` 返回值就相同——这印证了「哈希的是字节、而非类型」与「确定性」两点。

> 删除你临时加的断言，不要把改动提交（本讲禁止修改源码）。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接用 `std::hash::DefaultHasher`？

**答案**：标准库的 `DefaultHasher`（基于 SipHash）**每次新进程会用随机种子**，导致同一字符串在不同编译/运行中哈希值不同。而短键要求「跨机器、跨编译完全一致」，所以必须用固定密钥的 `SipHasher13::new()`。

**练习 2**：`SipHasher13` 的「13」是什么意思？

**答案**：压缩阶段 1 轮、终结阶段 3 轮。轮次比标准 SipHash-2-4 少，因而更快，安全性略低，但对「确定性 ID 生成」场景是合适取舍。

---

### 4.3 base62：128 位整数到可读短串

#### 4.3.1 概念说明

`hash128` 给出的是一个 `u128` 整数，但它不能直接当 key——一来它是个数不是字符串，二来直接转十进制会有 39 位之长。`base62` 把这个整数编码成用 **62 个符号**（`0-9`、`A-Z`、`a-z`）表示的字符串。62 进制比 10 进制紧凑得多，比 16 进制更紧凑，而且所有符号都是 URL 安全、可打印的 ASCII，适合做键名。

#### 4.3.2 核心流程

128 位数用 base62 表示需要多少位？设需要 \(d\) 位，则需满足：

\[
62^{d-1} \le 2^{128} - 1 < 62^{d}
\]

取以 62 为底的对数：

\[
d = \left\lceil \log_{62}(2^{128}) \right\rceil = \left\lceil 128 \cdot \frac{\ln 2}{\ln 62} \right\rceil \approx \lceil 21.495 \rceil = 22
\]

也就是说，**128 位哈希的 base62 编码最多 22 个字符**。可以验证：

\[
62^{21} \approx 4.4 \times 10^{37} < 2^{128} \approx 3.4 \times 10^{38} < 62^{22} \approx 2.7 \times 10^{39}
\]

由此得到三个推论：

1. 短键长度上限是 22。默认 `len = 24` 比 22 大，所以 `.min(encoded.len())` 几乎总把 24 钳到 22（或更小）——**默认配置下短键保留全部 128 位信息，没有信息损失**。
2. 编码长度其实会**轻微浮动**：当哈希值恰好较小时，可能只需 21 个字符就能表示。源码测试里 `"1"` 的短键 `"knx7vOJBRfzgQvNfEkbEi"` 就是 21 个字符。这正是 `.min()` 必须存在的实证。
3. 若把 `len` 调小（例如基准测试 [benches/minify_key.rs:4](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/minify_key.rs#L4) 用 `minify_key_len = 12`），就是把 22 位截到 12 位，key 更短但碰撞概率上升（key 空间从 \(62^{22}\) 缩到 \(62^{12} \approx 3.2 \times 10^{21}\)）。这是「长度 vs 碰撞」的权衡。

#### 4.3.3 源码精读

base62 的调用点就一行：

[crates/support/src/minify_key.rs:43](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs#L43) —— `let encoded = base62::encode(hash128(value));`：把上一步的 `u128` 哈希直接交给 `base62::encode`，得到一个 `String`。这里 `base62` 由 [Cargo.toml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/Cargo.toml) 的 workspace 依赖引入（声明下限 `base62 = "2.0.2"`），它原生支持把 `u128` 编码成 base62 字符串。

紧接着的两行完成截断与加前缀：

[crates/support/src/minify_key.rs:44-45](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs#L44-L45) —— `let len = len.min(encoded.len()); format!("{}{}", prefix, &encoded[..len]).into()`：先用 `.min()` 钳制长度，再切片取前 `len` 个字符，最后用 `format!` 拼上前缀并 `.into()` 转成 `Cow::Owned`。前缀（如 `t_`）只是简单字符串拼接，用于把自动生成的短键和人工写的键区分开，避免命名冲突。

#### 4.3.4 代码实践

**实践目标**：用源码自带测试值，反向验证「22 位上限」与「长度浮动」。

**操作步骤**：

1. 打开 [crates/support/src/minify_key.rs:113-160](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs#L113-L160) 的 `test_minify_key`。
2. 数一数两个断言值各自的字符数：
   - `"1LokVzuiIrh1xByyZG4wjZ"`（`"Hello, world!"` 的短键）
   - `"knx7vOJBRfzgQvNfEkbEi"`（`"1"` 的短键）

**需要观察的现象**：前者 22 个字符，后者 21 个字符。

**预期结果**：两个值都 ≤ 22，印证了 4.3.2 推导的「128 位 base62 最多 22 位」；两者长度不同，印证「哈希值较小时编码更短」，也解释了为什么函数里必须用 `.min()` 而不能直接 `encoded[..len]`。

#### 4.3.5 小练习与答案

**练习 1**：为什么默认 `len=24` 而不是直接设成 `22`？

**答案**：`24 > 22`，保证 `.min(encoded.len())` 钳制后**始终保留全部 22 位**（甚至偶尔的 21 位也全保留），即默认配置下短键不丢失任何哈希信息。设成 22 也能工作，但 24 是一个对「理论上限」留有余量的保守值；一旦哪天哈希算法变化使编码变长，24 也能自然适配而无需改默认。

**练习 2**：把 `len` 从 24 改成 8，碰撞风险如何变化？

**答案**：key 空间从 \(62^{22}\) 缩到 \(62^{8} \approx 2.18 \times 10^{14}\)。由生日悖论，约在 \(\sqrt{62^{8}} \approx 4.7 \times 10^{7}\)（约 4700 万）个不同 key 时就有一半概率碰撞，风险显著上升。短键更短但碰撞更多，这是明显的权衡。

---

### 4.4 四个默认常量与参数含义

#### 4.4.1 概念说明

`minify_key` 的四个参数都有「默认值」，集中定义在文件顶部的一组 `pub const`。这些常量是 macro crate（`i18n!` / `_minify_key!`）与 support crate 共享的「事实单一来源」：宏侧引用它们作为 `Cargo.toml` 未配置时的兜底默认值（参见 u5-l1 关于三级优先级的讲解）。

#### 4.4.2 核心流程

四个常量一一对应 `minify_key` 的四个参数（`value` 除外，它是输入而非配置）：

| 常量 | 取值 | 对应参数 | 含义 |
| --- | --- | --- | --- |
| `DEFAULT_MINIFY_KEY` | `false` | （宏级总开关 `minify_key = ...`） | 默认**关闭**短键，需显式 `minify_key = true` 才启用 |
| `DEFAULT_MINIFY_KEY_LEN` | `24` | `len` | 短键最大长度，≥ 22 故默认不截断 |
| `DEFAULT_MINIFY_KEY_PREFIX` | `""` | `prefix` | 默认无前缀 |
| `DEFAULT_MINIFY_KEY_THRESH` | `127` | `threshold` | 文案长度 ≤ 127 字节时不压缩 |

关于 `DEFAULT_MINIFY_KEY = false`：它控制**整套短键机制是否启用**。默认关闭意味着：如果你不显式写 `minify_key = true`，`t!` 就直接拿原文当 key（即 u3 系列讲的行为），短键这套算法完全不介入。这是向后兼容的设计——短键是 v3.1.0 引入的可选优化，不应改变原有行为。

关于 `DEFAULT_MINIFY_KEY_THRESH = 127` 的取值：127 字节是个经验阈值——比它短的句子当 key 已经足够小，压缩收益不大却牺牲了可读性；比它长的句子（典型如一整段法律声明、长提示语）才值得压成短键。注意单元测试与本讲实践里用的 `threshold=128` 是测试场景下的取值，与默认常量 `127` 是两回事——只要 `value.len() <= threshold` 就返回原文，`"Hello, world!"`（13 字节）在 `threshold=128` 下当然命中短路。

#### 4.4.3 源码精读

四个常量紧凑地写在文件开头：

[crates/support/src/minify_key.rs:5-15](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs#L5-L15) —— 四个 `DEFAULT_MINIFY_KEY_*` 常量：`bool` 总开关默认 `false`、长度默认 `24`、前缀默认空串、阈值默认 `127`。它们都是 `pub const`，故 macro crate 可以直接 `use` 引用，保证「support 算法」与「宏侧默认配置」用的是同一组数值，不会漂移。

它们如何在真实项目中生效？看集成测试 [tests/i18n_minify_key.rs:1-8](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/i18n_minify_key.rs#L1-L8)：

```rust
rust_i18n::i18n!(
    "./tests/locales",
    minify_key = true,
    minify_key_len = 24,
    minify_key_prefix = "t_",
    minify_key_thresh = 4
);
```

这里把四个参数显式覆盖为 `true / 24 / "t_" / 4`，`i18n!` 在编译期据此生成四个静态量 `_RUST_I18N_MINIFY_KEY`、`_RUST_I18N_MINIFY_KEY_LEN`、`_RUST_I18N_MINIFY_KEY_PREFIX`、`_RUST_I18N_MINIFY_KEY_THRESH`，并最终透传给每次 `_tr!` 调用（详见 u6-l2、u6-l3）。若这些参数在 `i18n!` 里省略、`Cargo.toml` 里也没配，则回落到本讲的四个默认常量。

#### 4.4.4 代码实践

**实践目标**：理解「默认关闭 + 可覆盖」与 `tkv!` 的协作。

**操作步骤**：

1. 阅读 [tests/i18n_minify_key.rs:52-63](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/tests/i18n_minify_key.rs#L52-L63) 的 `test_tkv`。
2. 运行该集成测试（需单线程，原因见 u8-l4）：

   ```bash
   RUST_TEST_THREADS=1 cargo test --test i18n_minify_key
   ```

**需要观察的现象**：`tkv!("Hello, world!")` 返回的元组 `(key, msg)` 中，`key == "t_1LokVzuiIrh1xByyZG4wjZ"`、`msg == "Hello, world!"`；而 `tkv!("Hey")` 返回 `("Hey", "Hey")`。

**预期结果**：
- `"Hello, world!"`（13 字节）> 阈值 `4`，被压缩成 `t_` + `1LokVzuiIrh1xByyZG4wjZ`。
- `"Hey"`（3 字节）≤ 阈值 `4`，命中短路，key 与 msg 都是原文 `"Hey"`。

这同时验证了三件事：前缀 `t_` 被正确拼接；阈值 `4` 生效（而非默认 `127`）；短键值与 support 单元测试完全一致——**宏侧与算法侧用的是同一个 `minify_key` 函数**。

> 若环境无法运行，此结果为「待本地验证」；断言值直接取自测试源码。

#### 4.4.5 小练习与答案

**练习 1**：如果完全不写任何 `minify_key` 相关配置，`t!("some sentence")` 的 key 是什么？

**答案**：因为 `DEFAULT_MINIFY_KEY = false`，短键机制整体不启用，`t!` 直接拿原文 `"some sentence"` 当 key 去查表——即 u3 系列讲的默认行为。

**练习 2**：`DEFAULT_MINIFY_KEY_THRESH = 127`，但本讲实践里用 `threshold=128` 说「原样返回」，这两者矛盾吗？

**答案**：不矛盾。`127` 是**未配置时的兜底默认值**；`128` 是**测试/实践中显式传入的参数值**。判定规则始终是 `value.len() <= threshold`：`"Hello, world!"` 长 13 字节，无论阈值是 127 还是 128，都满足 `13 <= 阈值`，故都返回原文。把阈值从 127 改成 128 只是多放行一个字节的文案，对本例结果无影响。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「从头算到尾」的小任务。

**任务**：给定字符串 `"Hello, world!"` 与 `threshold = 0`，预测并验证它生成的短键；再解释 `threshold = 128` 时为何原样返回。

**操作步骤**：

1. **预测**（不运行，先算）：
   - `"Hello, world!"` 是 13 个 ASCII 字节。
   - `threshold = 0` 时，`13 <= 0` 为假，**不短路**，进入压缩分支。
   - 压缩结果 = `prefix("") + base62(hash128("Hello, world!"))[..min(24, 22)]`。
   - 查源码测试可得预期值 `1LokVzuiIrh1xByyZG4wjZ`（22 个字符）。
2. **验证**（运行 support 单元测试）：

   ```bash
   cargo test -p rust-i18n-support --lib minify_key::tests::test_minify_key
   ```

3. **解释 `threshold = 128`**：此时 `13 <= 128` 为真，命中短路分支，直接返回 `Cow::Borrowed("Hello, world!")`，**完全不做哈希、不做 base62、不分配内存**。这正是测试第 132-135 行的断言。

**预期结果**：步骤 2 测试通过，断言值与步骤 1 的预测一致；步骤 3 的断言 `msg.minify_key(24, "", 128) == "Hello, world!"` 成立。

**延伸思考（可选）**：把同一个句子改成带前缀 `t_`（即 `minify_key(msg, 24, "t_", 0)`），预测结果应为 `t_1LokVzuiIrh1xByyZG4wjZ`——这与 `tests/i18n_minify_key.rs` 里 `tkv!("Hello, world!")` 的结果一致，说明前缀只是简单拼接、不影响哈希本身。可用 `cargo test --test i18n_minify_key`（加 `RUST_TEST_THREADS=1`）一并验证。

> 若本地缺少编译环境，上述运行结果标注为「待本地验证」；所有断言值均直接引自源码，可先通过阅读源码确认逻辑。

## 6. 本讲小结

- **短键（minify_key）解决「文案即 key」的体积与性能问题**：把长文案压成定长短串当 key，译文照旧，但 key 变小、哈希与比较更快、二进制更省。
- **`minify_key(value, len, prefix, threshold)` 是入口**：`value.len() <= threshold` 时返回 `Cow::Borrowed` 原文（零分配）；否则走「哈希 + base62 + 截断 + 前缀」。
- **哈希用固定密钥的 `SipHasher13`，输出 128 位**：`hash128` 哈希字符串的 UTF-8 字节，`LazyLock` 全局复用一个默认密钥实例，保证跨机器、跨编译的完全确定性。
- **base62 把 128 位整数编码成最多 22 个字符**：\( \lceil \log_{62} 2^{128} \rceil = 22 \)；默认 `len=24` 比它大，故 `.min()` 几乎总把 24 钳到 22，默认不损失信息。
- **`.min(encoded.len())` 是必要的防越界保护**：因为编码长度会在 21～22 之间轻微浮动（如 `"1"` 的短键是 21 位）。
- **四个 `DEFAULT_MINIFY_KEY_*` 常量是 macro 与 support 共享的兜底默认值**：总开关默认 `false`（向后兼容，默认不启用）、`len=24`、`prefix=""`、`thresh=127`；显式配置或 `Cargo.toml` 可覆盖。

## 7. 下一步学习建议

本讲只讲了**纯算法**。短键机制还有两块没展开，建议按顺序继续：

1. **u6-l2 `_minify_key!` 与 `tkv!` 宏**：看 `_minify_key!` 过程宏如何在**编译期**调用本讲的 `minify_key` 函数，把字面量句子直接算成常量 key token；以及 `tkv!` 如何生成 `(key, msg)` 元组，实现「长文案只写一次」。
2. **u6-l3 短键在 `t!` 与提取器中的协作**：看 `_tr!` 对字面量 / 元组 / 动态值三种输入的不同处理分支，以及为什么动态值只能在**运行时**调用 `minify_key`（更耗 CPU），并验证提取器与 `t!` 用同一套算法、同一组参数，保证 key 一致。

此外，若想看短键在性能基准中的真实开销，可在学完 u6-l2 后阅读 [benches/minify_key.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/benches/minify_key.rs)（它用 `minify_key_len = 12` 演示截断配置），相关基准方法在 u8-l2 详述。
