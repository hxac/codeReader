# 对象哈希与松散对象存储

> 承接上一讲（u3-l1）：我们已经在内存里认识了 blob/tree/commit/tag 四种对象、`struct object` 统一基类与对象标志位。本讲回答下一个自然的问题——**这些对象到底以什么名字、什么格式落到磁盘上？** 答案是：以「内容哈希」为名，以「zlib 压缩」为体，一个对象一个文件，存放在 `.git/objects/` 下。这就是「松散对象（loose object）」。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 git 为什么用 `git hash-object` 算出的哈希**不等于** `sha1sum` 算出的哈希，并能手动复现这个哈希值。
2. 画出「一段文件内容 → 对象哈希 → `.git/objects/xx/yyyy…` 文件」的完整数据流，指出每一步在源码里的函数。
3. 解释松散对象文件的目录分片（fan-out）规则、zlib 压缩格式，以及「先写临时文件再原子链接」的写入策略。
4. 看懂 `struct git_hash_algo` 这张「哈希算法虚表」，理解 git 如何在 SHA-1 与 SHA-256 之间抽象与切换。

---

## 2. 前置知识

本讲假设你已经了解（来自 u3-l1）：

- **对象类型** `enum object_type`：`OBJ_BLOB`、`OBJ_TREE`、`OBJ_COMMIT`、`OBJ_TAG`。
- **对象 ID（OID）**：对象的全局唯一名字，本质是一段哈希值。

本讲还要用到几个通俗概念，先在此解释：

- **内容寻址（content-addressable）**：传统文件系统里，文件名和内容互不相关（你可以把 `hello.txt` 改名叫 `a.txt`，内容不变）。git 反过来——**内容由哈希决定，哈希即文件名**。只要内容一样，名字必然一样；改一个字节，名字就彻底变了。
- **哈希（hash）**：把任意长度的数据「揉」成一个固定长度的指纹。git 默认用 SHA-1（20 字节，写成 40 个十六进制字符），也可用 SHA-256（32 字节，64 个十六进制字符）。
- **zlib 压缩**：一种无损压缩算法。git 用它把对象内容压扁后再落盘，省空间。注意它和 `.gz`/gzip 不同——git 用的是**裸 zlib 流**，没有 gzip 的文件头。
- **松散对象（loose object）vs 打包对象（packed object）**：一个对象单独占一个文件，叫「松散」；许多对象挤进一个 `.pack` 文件（见 u3-l3），叫「打包」。本讲只讲松散形态。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hash.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/hash.h) | 哈希算法的「抽象层」。定义 `struct object_id`、`struct git_hash_algo` 虚表、SHA-1/SHA-256 的长度常量与默认算法选择。 |
| [hash.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/hash.c) | 把具体的 SHA-1/SHA-256 实现登记进 `hash_algos[]` 数组，供运行时按编号取用。 |
| [object-file.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object-file.c) | **本讲的主战场**。对象的哈希计算（`hash_object_file`）、头部格式化（`format_object_header`）、松散对象写入（`write_loose_object`）、路径分片（`fill_loose_path`）、读取解压（`unpack_loose_header` 等）几乎都在这里。 |
| [object-file.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object-file.h) | 上述函数的声明，以及 `MAX_HEADER_LEN` 等常量。 |
| [odb/source-loose.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/odb/source-loose.c) | 对象数据库（ODB）的「松散后端」：负责把磁盘上的松散对象 `mmap` 进来、解压头部、读出内容。 |
| [odb/source-inmemory.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/odb/source-inmemory.c) | 一个「只算哈希、不落盘」的内存后端，用来对照说明「哈希」与「写入」是分离的两件事。 |
| [loose.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/loose.c) | ⚠️ 注意：在本版本里 `loose.c` **不是**松散对象的读写实现，而是维护 `objects/loose-object-idx` 索引，用于在「双哈希（compat hash）」仓库里做 SHA-1 ↔ SHA-256 的对象名翻译。松散对象的真正读写在上面的 `object-file.c` 与 `odb/source-loose.c`。把它列在这里是为了避免你被文件名误导。 |
| [Makefile](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile) | 决定编译期选用哪个 SHA-1 底层实现（默认是带碰撞检测的 `sha1dc`）。 |

> 关于规格里提到的 `sha1/sha1dgst.c`：当前 HEAD 已不存在该文件。git 现在的默认 SHA-1 实现位于 [sha1dc/sha1.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sha1dc/sha1.c)，并通过 [sha1dc_git.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sha1dc_git.h) 接入。本讲据此讲解，不引用不存在的文件。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 对象哈希与写入**——内容如何变成 OID、如何落盘。
2. **4.2 松散对象的压缩与读取**——磁盘文件的具体格式与读写流程。
3. **4.3 哈希算法抽象 `git_hash_algo`**——SHA-1/SHA-256 的统一接口。

---

### 4.1 对象哈希与写入：从内容到 OID 再到文件

#### 4.1.1 概念说明

很多人第一次用 git 时会被一个现象困扰：

```bash
$ echo "hello world" > greeting.txt
$ cat greeting.txt | sha1sum
1093e1f04BC...   # （举例）sha1sum 算出的值
$ git hash-object greeting.txt
3b18e512dba79e4c8300dd08aeb37f8e728b8dad   # git 算出的值，完全不同！
```

为什么不一样？因为 **git 哈希的不是「裸文件内容」，而是「头部 + 内容」拼接后的结果**。头部是一行：

```
<类型> <字节数><NUL>
```

对一个内容为 `hello world\n`（12 字节）的 blob，git 实际喂给哈希函数的字节流是：

```
blob 12\0hello world\n
        ^^          ^^
        空格         NUL 字节（值为 0，不是字符 '0'）
```

也就是说：先写类型名 `blob`，空格，十进制长度 `12`，一个值为 0 的字节（`\0`），**然后**才是原始内容。SHA-1 就作用在这整段字节流上。

这个设计有两个好处：

- **自描述**：光看哈希前的内容就能知道类型和大小，不用额外的元数据。
- **防混淆**：一段同样的字节，作为 blob 和作为 commit，哈希必然不同，不会把文件内容误当成提交。

#### 4.1.2 核心流程

一次「内容 → OID → 落盘」的流程可以用下面的伪代码刻画：

```text
hash_object(内容 buf, 长度 len, 类型 type) -> oid:
    hdr = "<type> <len>\0"                 # 1. 生成头部
    oid  = SHA( hdr || buf )               # 2. 哈希 = 头部拼内容
    return oid

write_loose(内容 buf, 长度 len, 类型 type) -> oid:
    hdr  = "<type> <len>\0"
    oid  = SHA( hdr || buf )               # 先算名字
    path = ".git/objects/" + oid[0:2] + "/" + oid[2:]   # 3. 分片路径
    data = zlib_deflate( hdr || buf )      # 4. 压缩「头部+内容」
    写 data 到 path 下的临时文件             # 5. tmp_obj_XXXXXX
    原子地把临时文件链接/改名到 path          # 6. finalize
    return oid
```

关键点：**被哈希的字节流 和 被压缩落盘的字节流是同一份「头部+内容」**。所以只要你能解压磁盘上的松散对象，重新哈希就能验证它没坏——这就是 git 的「自校验」。

#### 4.1.3 源码精读

**(a) 头部格式化** —— [object-file.c:90-99](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object-file.c#L90-L99)：用 `xsnprintf` 写出 `"<type> <len>"`，返回值再 `+1`，那个 `+1` 就是为末尾的 NUL 字节预留的。

```c
int format_object_header(char *str, size_t size, enum object_type type,
                         size_t objsize)
{
    const char *name = type_name(type);          // "blob" / "tree" / ...
    if (!name)
        BUG("could not get a type name for ...");
    return xsnprintf(str, size, "%s %"PRIuMAX,
                     name, (uintmax_t)objsize) + 1;   // +1 = NUL
}
```

**(b) 哈希本体** —— [object-file.c:318-327](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object-file.c#L318-L327)：这就是上面伪代码里 `SHA(hdr || buf)` 的真实实现。注意它**先 update 头部、再 update 内容**，两段分开喂给哈希函数，效果等价于拼接：

```c
static void hash_object_body(const struct git_hash_algo *algo,
                             struct git_hash_ctx *c,
                             const void *buf, size_t len,
                             struct object_id *oid,
                             char *hdr, size_t *hdrlen)
{
    algo->init_fn(c);                // 1. 初始化哈希上下文
    git_hash_update(c, hdr, *hdrlen); // 2. 喂入头部
    git_hash_update(c, buf, len);     // 3. 喂入内容
    git_hash_final_oid(oid, c);       // 4. 收尾，得到 OID
}
```

**(c) 公开的「只算哈希」入口** —— [object-file.c:474-482](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object-file.c#L474-L482)：`hash_object_file()` 是最常用的哈希 API。它先 `format_object_header` 生成头部，再交给 `write_object_file_prepare`（名字里有 "write" 但其实只算哈希、不落盘）：

```c
void hash_object_file(const struct git_hash_algo *algo, const void *buf,
                      size_t len, enum object_type type,
                      struct object_id *oid)
{
    char hdr[MAX_HEADER_LEN];            // #define MAX_HEADER_LEN 32
    size_t hdrlen = sizeof(hdr);
    write_object_file_prepare(algo, buf, len, type, oid, hdr, &hdrlen);
}
```

> 命名小坑：历史上 git 有一个 `write_object_file()` 函数「既算哈希又落盘」。在当前 HEAD 它已被重构进对象数据库（ODB）层（见模块 4.1 末尾的「写入分发」）。现在保留名字的只有 `write_object_file_prepare`（算哈希）和 `hash_object_file`（也算哈希）。**真正落盘的入口是 `write_loose_object`（模块 4.2）**。

**(d) 算哈希 vs 落盘的分叉点** —— [object-file.c:1018-1021](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object-file.c#L1018-L1021)：`git hash-object` 命令最终走到 `index_mem()`。这里用一个标志位 `INDEX_WRITE_OBJECT` 决定「只算哈希」还是「写入对象库」：

```c
if (write_object)
    ret = odb_write_object(istate->repo->objects, buf, size, type, oid);  // -w：落盘
else
    hash_object_file(istate->repo->hash_algo, buf, size, type, oid);      // 不带 -w：只算哈希
```

也就是说：`git hash-object greeting.txt`（不带 `-w`）只打印哈希、不创建文件；`git hash-object -w greeting.txt` 才会把对象写进 `.git/objects`。这正是 `hash_object_file`（纯哈希）与 `odb_write_object`（写入）的分工。

**(e) 写入分发到后端** —— [odb.c:988-997](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/odb.c#L988-L997)：`odb_write_object_ext` 把写入请求转发给 ODB 的「源（source）」。源可以是会落盘的「松散后端」，也可以是只算哈希的「内存后端」[odb/source-inmemory.c:229-239](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/odb/source-inmemory.c#L229-L239)——后者干脆直接调用 `hash_object_file`，什么也不写盘：

```c
/* 内存后端：只算 OID，不持久化 */
static int odb_source_inmemory_write_object(struct odb_source *source, ...) {
    ...
    hash_object_file(source->odb->repo->hash_algo, buf, len, type, oid);
    ...
}
```

这条对照线很能说明问题：**「算哈希」是底层原子能力，「落盘」是后端策略**，二者通过 `hash_object_file` 这个共用原语衔接。

#### 4.1.4 代码实践

**目标**：亲手验证「git 的哈希 = SHA-1(头部 + 内容)」，从而理解头部的作用。

**操作步骤**（需要已编译的 git 与 Python 3）：

1. 准备一个内容确定的小文件：

   ```bash
   printf 'hello world\n' > /tmp/greet.txt
   ```

2. 用 git 算哈希，并对照 `sha1sum`：

   ```bash
   git hash-object /tmp/greet.txt        # 期望：3b18e512dba79e4c8300dd08aeb37f8e728b8dad
   sha1sum /tmp/greet.txt                # 与上面不同
   ```

3. 用 Python 手动复现 git 的哈希——关键就是手动拼上 `blob 12\0` 头部：

   ```python
   import hashlib
   data = open('/tmp/greet.txt','rb').read()   # b'hello world\n'，长度 12
   header = b'blob %d\0' % len(data)           # b'blob 12\0'
   print(hashlib.sha1(header + data).hexdigest())
   # 期望与 git hash-object 输出完全一致
   ```

**需要观察的现象**：第 3 步 Python 算出的 SHA-1 与第 2 步 `git hash-object` 完全相同，而与 `sha1sum` 不同。这证明 git 哈希的是「头部 + 内容」。

**预期结果**：手动拼接头部后得到的哈希 `3b18e512dba79e4c8300dd08aeb37f8e728b8dad` 与 `git hash-object` 一致。

> 若你的环境没有 Python，也可以用一行 shell：`{ printf 'blob 12\0'; cat /tmp/greet.txt; } | sha1sum`（注意 `printf` 的 `\0` 会被解释成 NUL 字节）。

#### 4.1.5 小练习与答案

**练习 1**：空字符串（0 字节）作为 blob 的哈希是多少？为什么 git 把它「内置」成一个常量？

参考答案：哈希是 `e69de29bb2d1d6434b8b29ae775ad8c2e48c5391`。因为内容为空时，被哈希的字节流恒为 `blob 0\0`（没有内容部分），其 SHA-1 是确定值。git 把它直接写死成 `empty_blob_oid` 常量，见 [hash.c:12-18](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/hash.c#L12-L18)，这样空文件不需要真去算一次哈希。

**练习 2**：如果把同一段字节 `hello world\n` 当作 `commit` 类型去哈希（`git hash-object -t commit`），结果会跟当作 `blob` 一样吗？

参考答案：不一样。因为头部变成了 `commit 12\0...` 而非 `blob 12\0...`，被哈希的字节流不同，哈希必然不同。这也体现了头部「防混淆」的作用。

---

### 4.2 松散对象的压缩与读取

#### 4.2.1 概念说明

算出 OID 之后，对象要落到磁盘。git 选择「一个对象一个文件」，文件名就是 OID，但做了两层处理：

1. **目录分片（fan-out）**：把 40 位十六进制哈希切成「前 2 位 + 后 38 位」。前 2 位作子目录名，后 38 位作文件名。例如 OID `3b18e512...` 存成 `.git/objects/3b/18e512dba79e4c8300dd08aeb37f8e728b8dad`。

   为什么要分片？因为一个大仓库可能有上千万个对象。若全堆在 `objects/` 一层目录下，单目录条目过多，绝大多数文件系统的目录查找会退化成线性扫描。分到 256 个子目录（`00`–`ff`）后，每个子目录平均只承载体量的 \( \tfrac{1}{256} \)。

   \[
   \text{每个子目录平均对象数} \approx \frac{N}{256}
   \]

2. **zlib 压缩**：磁盘文件的内容，是把「头部 + 原始内容」（也就是 4.1 里被哈希的那同一份字节流）做 zlib deflate 压缩后的结果。压缩不是必须的（不压缩也能工作），但能显著省空间，尤其对文本。

一句话总结松散对象的磁盘格式：

```text
.git/objects/<oid前2位>/<oid后38位>   <-- 文件名即 OID（分片）
    内容 = zlib_deflate( "<type> <len>\0" || 原始内容 )
```

#### 4.2.2 核心流程

**写入流程**（`write_loose_object`，[object-file.c:750-808](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object-file.c#L750-L808)）：

```text
1. odb_loose_path(oid)        -> 算出最终路径 objects/xx/yyyy...
2. create_tmpfile()           -> 在同目录建临时文件 tmp_obj_XXXXXX（0444 只读权限）
3. git_deflate_init()         -> 初始化 zlib 压缩流，同时初始化一个哈希上下文 c
4. 把头部 hdr 喂进压缩流，并 git_hash_update(c, hdr)
5. 把内容 buf 喂进压缩流（循环 deflate 直到 Z_STREAM_END），边压边写 fd，边 git_hash_update(c, buf)
6. git_hash_final_oid(parano_oid) -> 用「边压边算」的方式重新得到一个 oid
7. 比对 parano_oid 与预期 oid：不一致则 die("confused by unstable object source data")
8. close_loose_object(): fsync + close
9. finalize_object_file_flags(): 把临时文件硬链接到最终路径；若已存在则做碰撞检查
```

这里有三个值得专门点出的设计：

- **临时文件 + 原子链接**：先写 `tmp_obj_XXXXXX`，全部写完、校验通过后，才 `link()` 到正式文件名。这样即使写入中途崩溃，也只会留下一个临时垃圾文件，不会产生「半个对象」。
- **边压边哈希（paranoid rehash）**：第 6–7 步在压缩写入的同时，把「头部 + 内容」重新哈希一遍，与调用方传入的 `oid` 比对。这是一种防自身 bug 的保险——万一数据源在哈希与写入之间被改动了（unstable source），会立即报错。
- **硬链接优先、改名回退**：`finalize_object_file_flags` 默认用 `link()`（硬链接）把临时文件「克隆」成正式文件，因为硬链接是原子的且能顺便检测「目标已存在」。某些文件系统（FAT、Coda、跨目录）不支持硬链接，则回退到 `rename()`。

**读取流程**（`read_object_info_from_path`，[odb/source-loose.c:63-192](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/odb/source-loose.c#L63-L192)）基本是写入的逆过程：

```text
1. open(path) + mmap 把整个文件映射进内存
2. unpack_loose_header(): git_inflate 解压开头一小段，定位到 NUL，得到头部 "<type> <len>"
3. parse_loose_header(): 手工解析头部，取出 type 和 size
4. (可选) unpack_loose_rest(): 继续解压出完整内容
5. (校验) check_object_signature(): 把解压出的「头部+内容」重新哈希，与文件名(oid)比对，检测损坏
```

#### 4.2.3 源码精读

**(a) 路径分片** —— [object-file.c:43-55](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object-file.c#L43-L55)：逐字节把哈希转成十六进制，**在第一个字节（前两个十六进制字符）之后插一个 `/`**，这就是「前 2 位作目录」的由来：

```c
static void fill_loose_path(struct strbuf *buf, const struct object_id *oid,
                            const struct git_hash_algo *algop)
{
    for (size_t i = 0; i < algop->rawsz; i++) {
        static char hex[] = "0123456789abcdef";
        unsigned int val = oid->hash[i];
        strbuf_addch(buf, hex[val >> 4]);    // 高 4 位
        strbuf_addch(buf, hex[val & 0xf]);   // 低 4 位
        if (!i)
            strbuf_addch(buf, '/');          // 仅在第 0 字节后插 '/'
    }
}
```

注意循环上界是 `algop->rawsz`：SHA-1 是 20 字节，SHA-256 是 32 字节。也就是说**分片规则与哈希算法绑定**，换算法时路径深度自然跟着变。外层包装 [object-file.c:57-66](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object-file.c#L57-L66) `odb_loose_path()` 只是在前面拼上 `objects` 目录前缀。

**(b) 写入主循环** —— [object-file.c:750-808](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object-file.c#L750-L808)（节选关键行）：

```c
int write_loose_object(struct odb_source_loose *loose,
                       const struct object_id *oid, char *hdr,
                       int hdrlen, const void *buf, unsigned long len,
                       time_t mtime, unsigned flags)
{
    ...
    odb_loose_path(loose, &filename, oid);                 // 最终路径
    fd = start_loose_object_common(loose, &tmp_file, ...); // 建 tmpfile + 初始化压缩/哈希
    ...
    stream.next_in = (void *)buf;
    stream.avail_in = len;
    do {
        ret = write_loose_object_common(loose, &c, NULL, &stream, 1, in0, fd,
                                        compressed, sizeof(compressed)); // 边压边写边哈希
    } while (ret == Z_OK);
    ...
    ret = end_loose_object_common(loose, &c, NULL, &stream, &parano_oid, NULL); // 收尾哈希
    if (!oideq(oid, &parano_oid))
        die(_("confused by unstable object source data for %s"), ...);        // 自检
    close_loose_object(loose, fd, tmp_file.buf);                              // fsync+close
    return finalize_object_file_flags(..., tmp_file.buf, filename.buf, ...);  // 原子链接
}
```

辅助函数 `start_loose_object_common` [object-file.c:654-698](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object-file.c#L654-L698) 同时完成三件事：建临时文件、`git_deflate_init` 初始化压缩、把头部先喂进压缩流并 `git_hash_update`。`write_loose_object_common` [object-file.c:704-724](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object-file.c#L704-L724) 每轮 `git_deflate` 一块、`git_hash_update` 一块、`write_in_full` 写一块——注意「哈希、压缩、写盘」三者是**交织在同一段数据上**进行的，所以第 6 步的 `parano_oid` 与最初算出的 `oid` 若不一致，就说明数据在中途被动过。

**(c) 原子落盘** —— [object-file.c:408-472](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object-file.c#L408-L472) `finalize_object_file_flags()`：默认走 `link(tmpfile, filename)`；若返回 `EEXIST`（目标已存在），说明同 OID 的对象已有人写过——因为同内容必然同 OID，此时通常是安全的重复写入，删除临时文件即可；若担心碰撞，可走 `check_collision()` 逐字节比对。

**(d) 读取解压** —— [odb/source-loose.c:63-192](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/odb/source-loose.c#L63-L192)（节选）：

```c
map = xmmap(NULL, mapsize, PROT_READ, MAP_PRIVATE, fd, 0);   // 整文件 mmap
...
switch (unpack_loose_header(&stream, map, mapsize, hdr, sizeof(hdr))) {  // 解压头部
case ULHR_OK:
    if (parse_loose_header(hdr, oi) < 0) ...                 // 解析 "<type> <len>"
    if (oi->contentp)
        *oi->contentp = unpack_loose_rest(&stream, hdr, *oi->sizep, oid); // 解压正文
    ...
}
```

头部解析 [object-file.c:262-316](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object-file.c#L262-L316) `parse_loose_header()` 是「手写的严格解析」——先读到空格拿到类型名，再逐字符解析十进制长度，并拒绝非规范写法（如前导零 `010`）。源码注释特意说明它比 `sscanf` 更严格。

**(e) 完整性校验** —— [object-file.c:101-112](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object-file.c#L101-L112) `check_object_signature()`：把读出来的内容用 `hash_object_file` 重算一遍，与期望 `oid` 比对，不一致即视为损坏。这就是 `git fsck` 检测对象损坏的底层依据。

#### 4.2.4 代码实践

**目标**：用一个真实写入的松散对象，验证「文件名 = OID」「文件内容 = zlib(头部 + 内容)」，并对照 `loose.c`/`fill_loose_path` 的分片规则。

**操作步骤**（需要一个测试仓库，例如 `git init /tmp/loose-demo && cd /tmp/loose-demo`）：

1. 写入一个对象并记录哈希：

   ```bash
   cd /tmp/loose-demo
   printf 'hello world\n' > greet.txt
   git hash-object -w greet.txt
   # 输出：3b18e512dba79e4c8300dd08aeb37f8e728b8dad
   ```

2. 按分片规则定位文件（前 2 位 `3b` 作目录，其余作文件名）：

   ```bash
   ls -l .git/objects/3b/18e512dba79e4c8300dd08aeb37f8e728b8dad
   # 文件存在，大小通常比 12 字节大（含头部+zlib 开销）
   ```

3. 用 git 自带工具解压查看（`-p` = pretty-print 内容，`-t` = 类型）：

   ```bash
   git cat-file -t 3b18e512                         # blob
   git cat-file -p 3b18e512                         # hello world
   git cat-file -s 3b18e512                         # 12（内容字节数）
   ```

4. 用 Python 直接解压磁盘文件，看到「头部 + 内容」原始字节流（即被哈希的那份）：

   ```python
   import zlib
   raw = open('.git/objects/3b/18e512dba79e4c8300dd08aeb37f8e728b8dad','rb').read()
   print(zlib.decompress(raw))   # b'blob 12\x00hello world\n'
   ```

5. （对照 `loose.c`）查看是否存在 `objects/loose-object-idx`：

   ```bash
   ls .git/objects/loose-object-idx 2>/dev/null || echo "不存在（普通 SHA-1 单哈希仓库不会有此文件）"
   ```

**需要观察的现象**：

- 第 2 步：文件确实出现在 `3b/` 子目录下，文件名是哈希的后 38 位——印证 `fill_loose_path` 的分片。
- 第 4 步：解压后得到 `blob 12\0hello world\n`，正好是 4.1 里被哈希的字节流。
- 第 5 步：普通仓库没有 `loose-object-idx`，说明 `loose.c` 维护的索引只在「双哈希」仓库（同时支持 SHA-1 与 SHA-256）里才启用。

**预期结果**：手动 `zlib.decompress` 的输出与 `git cat-file` 显示的内容一致；文件路径与哈希前缀吻合。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `write_loose_object` 要在压缩写入的同时「边算一遍哈希（parano_oid）」再和传入的 `oid` 比对，而不是直接信任传入的 `oid`？

参考答案：这是一种防御性自检。调用方传入的 `oid` 是更早算好的，而从那时起到真正写盘之间，数据源（buf）理论上有可能被改动（注释称之为 "unstable object source data"）。若内容变了但还用旧 oid 当文件名，就会造成「文件名与内容不匹配」的静默损坏。边写边重算并比对，能在第一时间 `die()`，避免脏数据落盘。

**练习 2**：`finalize_object_file_flags` 默认用 `link()`（硬链接）而不是 `rename()`，主要原因之一是什么？

参考答案：硬链接是原子操作，且当目标已存在时 `link()` 会失败并返回 `EEXIST`——这恰好让 git 察觉「这个对象已经有人写过了」。由于内容寻址保证了「同 OID 必同内容」，重复写入是安全的，删掉临时文件即可；同时还保留了对目标做逐字节碰撞检查（`check_collision`）的能力。某些文件系统不支持硬链接时，才回退到 `rename()`。

---

### 4.3 哈希算法抽象 `git_hash_algo`

#### 4.3.1 概念说明

git 诞生时只用 SHA-1。但 SHA-1 已被证明存在碰撞风险（Google 的 SHAttered 攻击），所以 git 一边给 SHA-1 加上「碰撞检测」，一边准备迁移到 SHA-256。这就要求**上层代码不能把 SHA-1 写死**——否则换算法要改遍几千处调用。

git 的解法是经典的面向对象手法：定义一张「虚表」`struct git_hash_algo`，里面放哈希算法的全部属性与方法（函数指针）：

| 字段 | 含义 |
| --- | --- |
| `name` | 算法名，如 `"sha1"`、`"sha256"`，用于配置与报错信息 |
| `format_id` | 4 字节标识（如 `"sha1"`），写入 pack 文件头用于识别 |
| `rawsz` / `hexsz` | 哈希的字节长度 / 十六进制字符长度（SHA-1：20/40；SHA-256：32/64） |
| `blksz` | 哈希的块大小（都是 64 字节） |
| `init_fn` / `update_fn` / `final_fn` / `clone_fn` | 哈希操作的函数指针 |
| `empty_tree` / `empty_blob` / `null_oid` | 该算法下的几个「著名」OID 常量 |

所有上层代码（如 4.1 的 `hash_object_body`）都通过 `algo->init_fn(c)`、`algo->update_fn(...)` 这样**通过指针间接调用**，从不直接调用 `SHA1_Init`。换算法 = 换一张虚表，调用代码一行不改。

#### 4.3.2 核心流程

算法的「注册」与「选用」分两层：

```text
编译期（Makefile）：
    选定 SHA-1 的底层实现 -> sha1dc(默认,带碰撞检测) / block-sha1 / openssl / apple
    选定 SHA-256 的底层实现 -> sha256/block(默认) / openssl / nettle / gcrypt
    通过 -DSHA1_DC 等宏，让 hash.h 里的 platform_SHA1_* 指向具体实现

链接期（hash.c）：
    把 sha1 / sha256 两套 init/update/final 包成 git_hash_sha1_* / git_hash_sha256_*
    登记进全局数组 hash_algos[GIT_HASH_NALGOS]：
        hash_algos[GIT_HASH_UNKNOWN] = { ...全是指向 BUG() 的函数 ... }
        hash_algos[GIT_HASH_SHA1]    = { "sha1",   ..., git_hash_sha1_*,   ... }
        hash_algos[GIT_HASH_SHA256]  = { "sha256", ..., git_hash_sha256_*, ... }

运行期（repository）：
    仓库初始化时根据 extensions.objectFormat 读取算法编号
    repo->hash_algo = &hash_algos[hash_algo]
    之后所有哈希都经 repo->hash_algo 间接调用
```

算法编号是简单整数：`GIT_HASH_SHA1 = 1`、`GIT_HASH_SHA256 = 2`（`0` 留给 UNKNOWN，调用它会触发 `BUG()`）。默认算法由 `GIT_HASH_DEFAULT` 决定——除非用 `WITH_BREAKING_CHANGES` 编译，否则仍是 SHA-1。

#### 4.3.3 源码精读

**(a) OID 与上下文结构** —— [hash.h:212-215](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/hash.h#L212-L215)：`struct object_id` 用最大长度（32 字节）的定长数组存放哈希，并附记 `algo` 编号，这样任何 OID 都能自报家门是哪种算法算出来的：

```c
struct object_id {
    unsigned char hash[GIT_MAX_RAWSZ];   /* GIT_MAX_RAWSZ = 32，按最大算法预留 */
    uint32_t algo;                        /* 是哪种算法：1=sha1, 2=sha256 */
};
```

[hash.h:259-267](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/hash.h#L259-L267) `struct git_hash_ctx` 是哈希计算的「进行中状态」，用 `union` 同时容纳 SHA-1 与 SHA-256 的底层上下文，省去分别分配：

```c
struct git_hash_ctx {
    const struct git_hash_algo *algop;   /* 指回所属算法虚表 */
    union {
        git_SHA_CTX sha1;
        git_SHA256_CTX sha256;
    } state;
};
```

**(b) 算法虚表** —— [hash.h:275-321](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/hash.h#L275-L321) `struct git_hash_algo` 就是上面表格里的那张表，外加 `extern const struct git_hash_algo hash_algos[GIT_HASH_NALGOS];` 这一全局数组声明。

**(c) 注册表** —— [hash.c:184-231](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/hash.c#L184-L231)：三个元素依次是 UNKNOWN（方法全指向 `BUG()`，防止误用）、SHA-1、SHA-256。以 SHA-1 项为例：

```c
{
    .name = "sha1",
    .format_id = GIT_SHA1_FORMAT_ID,        /* 0x73686131 == "sha1" 大端 */
    .rawsz = GIT_SHA1_RAWSZ,                 /* 20 */
    .hexsz = GIT_SHA1_HEXSZ,                 /* 40 */
    .blksz = GIT_SHA1_BLKSZ,                 /* 64 */
    .init_fn = git_hash_sha1_init,
    .update_fn = git_hash_sha1_update,
    .final_fn = git_hash_sha1_final,
    .final_oid_fn = git_hash_sha1_final_oid,
    .empty_tree = &empty_tree_oid,
    .empty_blob = &empty_blob_oid,
    .null_oid = &null_oid_sha1,
},
```

而 `git_hash_sha1_init` [hash.c:46-50](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/hash.c#L46-L50) 就是把虚表指针记进上下文，再调用底层 `git_SHA1_Init`：

```c
static void git_hash_sha1_init(struct git_hash_ctx *ctx)
{
    ctx->algop = &hash_algos[GIT_HASH_SHA1];
    git_SHA1_Init(&ctx->state.sha1);
}
```

**(d) 算法编号与默认值** —— [hash.h:169-183](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/hash.h#L169-L183)：

```c
#define GIT_HASH_UNKNOWN 0
#define GIT_HASH_SHA1 1
#define GIT_HASH_SHA256 2
#define GIT_HASH_NALGOS (GIT_HASH_SHA256 + 1)   /* 共 3 个槽位 */
...
#ifdef WITH_BREAKING_CHANGES
# define GIT_HASH_DEFAULT GIT_HASH_SHA256
#else
# define GIT_HASH_DEFAULT GIT_HASH_SHA1          /* 普通构建默认 SHA-1 */
#endif
```

**(e) 底层实现的选择（编译期）** —— [Makefile:2128-2163](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/Makefile#L2128-L2163)：SHA-1 实现按优先级四选一——`OPENSSL_SHA1` → `BLK_SHA1` → `APPLE_COMMON_CRYPTO_SHA1` → **默认 `SHA1_DC`**（带碰撞检测）：

```makefile
ifdef OPENSSL_SHA1
    ...
else
ifdef BLK_SHA1
    LIB_OBJS += block-sha1/sha1.o
    BASIC_CFLAGS += -DSHA1_BLK
else
ifdef APPLE_COMMON_CRYPTO_SHA1
    ...
else
    BASIC_CFLAGS += -DSHA1_DC            # 默认：碰撞检测版
    LIB_OBJS += sha1dc_git.o
    ...
    LIB_OBJS += sha1dc/sha1.o
    LIB_OBJS += sha1dc/ubc_check.o
```

`-DSHA1_DC` 让 [hash.h:14-16](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/hash.h#L14-L16) 引入 `sha1dc_git.h`，后者把 `platform_SHA1_Init` 等宏重定向到碰撞检测版（[sha1dc_git.h:24](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sha1dc_git.h#L24) `#define platform_SHA1_Init git_SHA1DCInit`）。于是整条调用链 `algo->init_fn → git_hash_sha1_init → git_SHA1_Init → platform_SHA1_Init → git_SHA1DCInit` 最终落到 [sha1dc/sha1.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sha1dc/sha1.c)。SHA-1 的碰撞检测对用户完全透明：发现疑似碰撞时，git 会拒绝写入并报错，从而保护仓库完整性。

#### 4.3.4 代码实践

**目标**：直观感受「同一份内容在不同算法下，OID 与目录深度都不同」，并理解默认算法仍是 SHA-1。

**操作步骤**：

1. 在默认（SHA-1）仓库里写一个对象：

   ```bash
   cd /tmp/loose-demo   # 之前的 SHA-1 仓库
   git hash-object -w greet.txt        # 3b18e512...（40 个十六进制字符）
   # 对象路径：.git/objects/3b/18e512dba79e4c8300dd08aeb37f8e728b8dad （20 字节哈希）
   ```

2. 新建一个 SHA-256 仓库，写入同样内容：

   ```bash
   git init --object-format=sha256 /tmp/sha256-demo
   cd /tmp/sha256-demo
   printf 'hello world\n' > greet.txt
   git hash-object -w greet.txt
   # 输出会是 64 个十六进制字符（如 bfa...），与 SHA-1 完全不同
   ```

3. 查看 SHA-256 仓库的对象路径与目录深度：

   ```bash
   find .git/objects -type f -not -path '*/pack/*' | head
   # 形如 .git/objects/<前2位>/<后62位>，文件名比 SHA-1 的长（32 字节哈希）
   ```

4. 确认默认算法：

   ```bash
   git -C /tmp/loose-demo rev-parse --show-object-format    # sha1
   git -C /tmp/sha256-demo rev-parse --show-object-format   # sha256
   ```

**需要观察的现象**：

- 同样的 `hello world\n`，SHA-256 仓库算出的 OID 更长（64 字符），且与 SHA-1 的 OID 毫无关系。
- SHA-256 仓库的松散对象路径，文件名部分更长（38 → 62 位十六进制），但「前 2 位作目录」的规则不变（因为 `fill_loose_path` 按 `rawsz` 循环，第 0 字节后总是插 `/`）。
- `rev-parse --show-object-format` 直观显示当前仓库用的是哪种算法。

**预期结果**：SHA-1 仓库报告 `sha1`，对象文件名 38 位；SHA-256 仓库报告 `sha256`，对象文件名 62 位。

> 若手头编译的 git 未启用 SHA-256（取决于编译选项），第 2 步可能失败。此时可改为「源码阅读型实践」：对照 [hash.h:192-204](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/hash.h#L192-L204) 的 `GIT_SHA1_RAWSZ(20)` / `GIT_SHA256_RAWSZ(32)`，手算「SHA-256 下 `fill_loose_path` 会生成几层目录、文件名多少位」，并说明为什么切换算法不需要改 `object-file.c` 的哈希逻辑。

#### 4.3.5 小练习与答案

**练习 1**：`hash_algos[GIT_HASH_UNKNOWN]` 的 `init_fn` 等函数都指向会触发 `BUG()` 的实现（[hash.c:138-166](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/hash.c#L138-L166)）。为什么要这样设计，而不是留空指针？

参考答案：留空指针的话，一旦误用 UNKNOWN 算法去哈希，会在调用处产生空指针解引用，崩溃位置离根因很远、难以排查。统一指向 `BUG()`，能在第一时间打印出「试图使用未知哈希算法」并带上栈回溯，把编程错误尽早暴露。

**练习 2**：假如未来 git 把默认算法从 SHA-1 切到 SHA-256，`object-file.c` 里 `hash_object_body`、`write_loose_object` 等函数需要大改吗？为什么？

参考答案：基本不用改。因为它们都是通过 `algo->init_fn` / `algo->update_fn` / `algo->final_fn` 间接调用哈希，路径长度等也读 `algop->rawsz`。只要 SHA-256 在 `hash_algos[]` 里正确注册（事实上已经注册），换默认算法只是改 `GIT_HASH_DEFAULT` 与仓库初始化时的选择，业务代码因依赖虚表而自动适配。这正是引入 `struct git_hash_algo` 抽象的回报。

---

## 5. 综合实践

把三个模块串起来，完成一次「手动模拟 git 写入一个松散对象」的端到端任务。

**任务**：在不使用 `git hash-object` 的前提下，用一段脚本（Python 或 shell）为一个文件「造出」一个 git 能识别的松散对象，并让 `git cat-file` 正常读出它。

**建议步骤**：

1. 选定内容（例如 `printf 'loose object demo\n' > demo.txt`），确定类型 `blob`。
2. 按 4.1 的公式构造头部 `blob <len>\0`，拼接内容，用 SHA-1 算出 OID（应与 `git hash-object demo.txt` 一致，可用来交叉验证）。
3. 把「头部 + 内容」做 zlib 压缩（Python `zlib.compress`），得到字节流。
4. 按 4.2 的分片规则，把结果写到 `.git/objects/<前2位>/<后38位>`（先建子目录，权限 0444）。
5. 用 `git cat-file -p <oid>` 与 `git cat-file -t <oid>` 验证 git 能读出内容和类型；再用 `git fsck` 确认没有报「对象损坏」。

**验收点**：

- 你手算的 OID 与 `git hash-object` 一致 → 证明你掌握了「头部 + 内容」的哈希规则（4.1）。
- git 能直接 `cat-file` 读出你手写的文件 → 证明你掌握了松散对象的路径与 zlib 格式（4.2）。
- 若你把脚本里的 `SHA-1` 换成 `SHA-256`、长度常量换成 32，并在一个 `--object-format=sha256` 仓库里重做，同样能成功 → 证明你理解了算法抽象（4.3）使这一切与具体算法无关。

> 注意：本实践是「在测试仓库里手动写对象」，请勿在生产仓库操作；写错格式会让 `git fsck` 报错。

---

## 6. 本讲小结

- git 对象的哈希作用于**「`<类型> <长度>\0` 头部 + 原始内容」**整段字节流，所以 `git hash-object` 与 `sha1sum` 结果不同；核心实现在 [object-file.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/object-file.c) 的 `format_object_header` → `hash_object_body` → `hash_object_file`。
- 「算哈希」与「落盘」是分离的两件事：`hash_object_file` 只算 OID，`odb_write_object` 才触发写入；内存后端 [odb/source-inmemory.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/odb/source-inmemory.c) 甚至只算哈希不写盘。
- 松散对象以**分片路径**（前 2 位作目录）存放，内容是**同一段「头部+内容」的 zlib 压缩**；写入走「临时文件 + 原子硬链接 + 自检重哈希」三保险（`write_loose_object` / `finalize_object_file_flags`）。
- 读取是写入的逆过程：`mmap` → `unpack_loose_header` → `parse_loose_header` → `unpack_loose_rest`，并可经 `check_object_signature` 重哈希校验完整性。
- 哈希算法通过**虚表 `struct git_hash_algo`** 抽象，登记在 `hash_algos[]`；默认仍是 SHA-1，底层默认用带碰撞检测的 `sha1dc`；切换到 SHA-256 不需要改业务代码。
- 规格里提到的 `loose.c` 在当前版本实为「双哈希对象名翻译索引」的维护代码，并非松散对象读写本体——读懂文件真实职责比记文件名更重要。

---

## 7. 下一步学习建议

本讲只讲了「单个对象怎么存」。一个仓库里动辄几百万对象，全用松散形态既慢又浪费空间。接下来建议：

1. **u3-l3 pack 文件格式与打包存储**：阅读 [packfile.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/packfile.c) 与 [pack-write.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pack-write.c)，理解 `git gc` 如何把大量松散对象合并、用 delta 压缩塞进单个 `.pack` 文件，以及 `.idx` 索引如何定位对象。
2. **回看 ODB 分层**：本讲多次出现 `odb_source`、`odb_write_object`，建议顺带浏览 [odb.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/odb.h) 与 [odb/source.h](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/odb/source.h)，建立「对象数据库 = 多个 source（松散/打包/内存/备用）串联」的整体观。
3. **进阶阅读**：想了解碰撞检测如何工作，可读 [sha1dc/ubc_check.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/sha1dc/ubc_check.c)（unchanged-bit-condition 检测）；想了解双哈希仓库的对象名翻译，可读 [loose.c](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/loose.c)。
