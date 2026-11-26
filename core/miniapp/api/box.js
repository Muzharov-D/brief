// Зашифрованный ящик мини-приложения. Сервер хранит только шифротекст: ключ есть на Mac владельца
// и в адресе кнопки его бота (после #, на сервер не уходит). Расшифровать день здесь нельзя.
//   PUT  /api/box?id=ID&part=day   Mac кладёт снимок дня. Заголовок X-Box-Key: секрет записи, id = sha256(секрет)[:32]
//   GET  /api/box?id=ID&part=day   мини-приложение берёт снимок
//   POST /api/box?id=ID&part=act   мини-приложение кладёт действие («задача готова»), тоже зашифрованное
//   GET  /api/box?id=ID&part=act   Mac забирает действия (с X-Box-Key), ящик действий очищается
const crypto = require('crypto');
const {put, get} = require('@vercel/blob');

const MAX_DAY = 400 * 1024, MAX_ACT = 4 * 1024, MAX_ACTS = 50;
const okId = id => /^[0-9a-f]{32}$/.test(id || '');
const owner = (req, id) => {
  const k = req.headers['x-box-key'] || '';
  return k.length >= 32 && crypto.createHash('sha256').update(k).digest('hex').slice(0, 32) === id;
};
async function readBody(req, max) {
  const chunks = []; let n = 0;
  for await (const c of req) { n += c.length; if (n > max) throw new Error('big'); chunks.push(c); }
  return Buffer.concat(chunks).toString('utf8');
}
async function load(path) {
  const r = await get(path, {access: 'private', useCache: false}).catch(() => null);
  if (!r || r.statusCode !== 200) return null;
  return await new Response(r.stream).text();
}
const save = (path, text) => put(path, text, {access: 'private', allowOverwrite: true, addRandomSuffix: false, contentType: 'application/json'});
const sealed = t => { try { const o = JSON.parse(t); return typeof o.iv === 'string' && typeof o.ct === 'string' ? o : null; } catch (e) { return null; } };

module.exports = async (req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  const {id, part} = req.query || {};
  if (!okId(id) || !['day', 'act'].includes(part)) return res.status(400).json({error: 'Неверный адрес ящика'});
  const path = `box/${id}/${part}.json`;
  try {
    if (part === 'day' && req.method === 'PUT') {
      if (!owner(req, id)) return res.status(403).json({error: 'Нет ключа записи'});
      const t = await readBody(req, MAX_DAY);
      if (!sealed(t)) return res.status(400).json({error: 'Ожидается шифротекст'});
      await save(path, t);
      return res.status(200).json({ok: true});
    }
    if (part === 'day' && req.method === 'GET') {
      const t = await load(path);
      if (!t) return res.status(404).json({error: 'День ещё не загружен с компьютера'});
      res.setHeader('Content-Type', 'application/json');
      return res.status(200).send(t);
    }
    if (part === 'act' && req.method === 'POST') {
      const o = sealed(await readBody(req, MAX_ACT));
      if (!o) return res.status(400).json({error: 'Ожидается шифротекст'});
      const list = JSON.parse((await load(path)) || '[]').slice(-(MAX_ACTS - 1));
      list.push({iv: o.iv, ct: o.ct, at: Date.now()});
      await save(path, JSON.stringify(list));
      return res.status(200).json({ok: true, queued: list.length});
    }
    if (part === 'act' && req.method === 'GET') {
      if (!owner(req, id)) return res.status(403).json({error: 'Нет ключа записи'});
      const t = (await load(path)) || '[]';
      if (t !== '[]') await save(path, '[]');
      res.setHeader('Content-Type', 'application/json');
      return res.status(200).send(t);
    }
    return res.status(405).json({error: 'Метод не поддерживается'});
  } catch (e) {
    return res.status(e.message === 'big' ? 413 : 500).json({error: e.message === 'big' ? 'Слишком большой' : 'Ошибка хранилища'});
  }
};
