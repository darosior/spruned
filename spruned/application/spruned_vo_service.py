import typing
import random
import binascii

from pycoin.block import Block
from pycoin.tx import Tx

from spruned.application.tools import deserialize_header
from spruned.application import settings, exceptions, utils
from spruned.application.abstracts import RPCAPIService, StorageInterface
from spruned.application.cache import cache_transaction
from spruned.application.logging_factory import Logger


class SprunedVOService(RPCAPIService):
    def __init__(self, electrod, p2p, cache=None, utxo_tracker=None, repository=None):
        self.sources = []
        self.primary = []
        self.cache = cache
        self.p2p = p2p
        self.electrod = electrod
        self.min_sources = 1
        self.current_best_height = None
        self.utxo_tracker = utxo_tracker
        self.repository = repository

    def available(self):
        raise NotImplementedError

    def _get_from_cache(self, *a):
        if self.cache:
            data = self.cache.get(a[0], a[1])
            if data:
                return data

    def add_cache(self, cache: StorageInterface):
        assert isinstance(cache, StorageInterface)
        self.cache = cache

    def add_source(self, service: RPCAPIService):
        assert isinstance(service, RPCAPIService)
        self.sources.append(service)

    async def getblock(self, blockhash: str, mode: int=1):
        block_header = self.repository.get_block_header(blockhash)
        if not block_header:
            return
        block = await self._get_block(block_header)
        if mode == 1:
            best_header = self.repository.get_best_header()
            block['confirmations'] = best_header['block_height'] - block_header['block_height']
            serialized = self._serialize_header(block_header)
            serialized['tx'] = [tx.id() for tx in block['block_object'].txs]
            return serialized
        elif mode == 2:
            raise NotImplementedError
        return binascii.hexlify(block['block_bytes']).decode()

    async def _get_block(self, blockheader, _r=0):
        blockhash = blockheader['block_hash']
        block = self.repository.get_block(blockhash) or await self.p2p.get_block(blockhash)
        if not block:
            if _r > 3:
                raise exceptions.ServiceException
            return await self._get_block(blockheader, _r+1)
        return block

    @cache_transaction
    async def getrawtransaction(self, txid: str, verbose=False):
        source = random.choice(self.sources)
        transaction = await source.getrawtransaction(txid, verbose)
        if not transaction:
            # design fixme
            return
        electrod_rawtx = await self.electrod.getrawtransaction(txid)
        transaction['rawtx'] = electrod_rawtx
        transaction['source'] += ', electrum'
        if transaction.get('blockhash'):
            blockheader = self.repository.get_block_header(transaction['blockhash'])
            merkleproof = await self.electrod.getmerkleproof(txid, blockheader['block_height'])
            assert merkleproof  # todo verify
        return transaction

    '''
    async def gettxout(self, txid: str, index: int):  # pragma: no cover
        """
        This entire part need a rethinking
        At the moment consider as we don't have a gettxout api available yet.
        """
        source = random.choice(self.sources)
        txout = await source.gettxout(txid, index)
        if not txout:
            return
        if not await self.ensure_unspent_consistency_with_electrum_network(txid, index, txout):
            return

        best_block_header = self.repository.get_best_header()
        tx_blockheader = self.repository.get_block_header(txout['in_block'])
        in_block_height = tx_blockheader['block_height']
        confirmations = best_block_header['block_height'] - in_block_height
        return {
            "bestblock": best_block_header['block_hash'],
            "confirmations": confirmations,
            "value": '{:.8f}'.format(txout['value_satoshi'] / 10**8),
            "scriptPubKey": {
                "asm": txout['script_asm'],
                "hex": txout['script_hex'],
                "reqSigs": "Not Implemented Yet",
                "type": txout["script_type"],
                "addresses": txout["addresses"]
            },
            "coinbase": "Not Implemented Yet"
        }

    async def ensure_unspent_consistency_with_electrum_network(
            self, txid: str, index: int, data: typing.Dict):   # pragma: no cover
        if not data['addresses']:
            if settings.ALLOW_UNSAFE_UTXO:
                return True
            return
        if data['script_type'] == 'nulldata':  # I'm not sure if nulldata can reach this point, investigate.
            return True
        found = False
        unspents = await self.electrod.listunspents(data['addresses'][0])
        if not unspents:
            return
        for unspent in unspents:
            if unspent['tx_hash'] == txid and unspent['tx_pos'] == index:
                found = unspent
                break
        if not data['unspent'] and found:
            Logger.third_party.debug(
                'unspent found in the electrum listunspent for the given address, '
                'but marked as spent by the other sources'
            )
            self.utxo_tracker and not data['unspent'] and self.utxo_tracker.invalidate_spent(txid, index)
            raise exceptions.SpentTxOutException

        if data['unspent'] and not found:
            if bool(data['value_satoshi'] != found['value']):
                raise exceptions.SpentTxOutException
        return True
    '''

    async def getbestblockhash(self):
        res = self.repository.get_best_header().get('block_hash')
        return res and res

    async def getblockhash(self, blockheight: int):
        return self.repository.get_block_hash(blockheight)

    async def getblockheader(self, blockhash: str, verbose=True):
        header = self.repository.get_block_header(blockhash)
        if verbose:
            _best_header = self.repository.get_best_header()
            res = self._serialize_header(header)
            res["confirmations"] = _best_header['block_height'] - header['block_height'] + 1
        else:
            res = binascii.hexlify(header['header_bytes']).decode()
        return res

    @staticmethod
    def _serialize_header(header):
        _deserialized_header = deserialize_header(binascii.hexlify(header['header_bytes']).decode())
        return {
            "hash": _deserialized_header['hash'],
            "height": header['block_height'],
            "version": _deserialized_header['version'],
            "versionHex": "Not Implemented Yet",
            "merkleroot": _deserialized_header['merkle_root'],
            "time": _deserialized_header['timestamp'],
            "mediantime": _deserialized_header['timestamp'],
            "nonce": _deserialized_header['nonce'],
            "bits": _deserialized_header['bits'],
            "difficulty": "Not Implemented Yet",
            "chainwork": "Not Implemented Yet",
            "previousblockhash": _deserialized_header['prev_block_hash'],
            "nextblockhash": header.get('next_block_hash')
        }

    async def getblockcount(self):
        return self.repository.get_best_header().get('block_height')

    async def estimatefee(self, blocks: int):
        return await self.electrod.estimatefee(blocks)

    async def getbestblockheader(self, verbose=True):
        best_header = self.repository.get_best_header()
        return await self.getblockheader(best_header['block_hash'], verbose=verbose)

    async def getblockchaininfo(self):
        best_header = self.repository.get_best_header()
        _deserialized_header = deserialize_header(best_header['header_bytes'])
        return {
            "chain": "main",
            "warning": "spruned v%s. emulating bitcoind v%s" % (settings.VERSION, settings.BITCOIND_API_VERSION),
            "blocks": best_header["block_height"],
            "headers": best_header["block_height"],
            "bestblockhash": best_header["block_hash"],
            "difficulty": None,
            "chainwork": None,
            "mediantime": _deserialized_header["timestamp"],
            "verificationprogress": 0,
            "pruned": False,
        }
