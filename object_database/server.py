#   Copyright 2018 Braxton Mckee
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

from object_database.messages import ClientToServer, ServerToClient, getHeartbeatInterval
from object_database.schema import Schema
from object_database.core_schema import core_schema
import object_database.keymapping as keymapping
from object_database.algebraic_protocol import AlgebraicProtocol
from typed_python.hash import sha_hash
from typed_python import *

import time
import uuid
import logging
import threading
import traceback
import json

tupleOfString = TupleOf(str)

class ConnectedChannel:
    def __init__(self, initial_tid, channel, connectionObject):
        self.channel = channel
        self.initial_tid = initial_tid
        self.connectionObject = connectionObject
        self.lastHeartbeat = time.time()
        self.definedSchemas = {}
        self.subscribedTypes = set() #schema, type
        self.subscribedIds = set() #identities
        self.subscribedIndexKeys = set() #full index keys

    def heartbeat(self):
        self.lastHeartbeat = time.time()

    def sendTransaction(self, msg):
        #we need to cut the transaction down
        self.channel.write(msg)

    def sendInitializationMessage(self):
        self.channel.write(
            ServerToClient.Initialize(transaction_num=self.initial_tid, connIdentity=self.connectionObject._identity)
            )

    def sendTransactionSuccess(self, guid, success):
        self.channel.write(
            ServerToClient.TransactionResult(transaction_guid=guid,success=success)
            )

class Server:
    def __init__(self, kvstore):
        self._lock = threading.RLock()
        self._kvstore = kvstore

        self.verbose = False

        self._removeOldDeadConnections()

        self._clientChannels = {}
        
        #id of the next transaction
        self._cur_transaction_num = 0

        #for each key, the last version number we committed
        self._version_numbers = {}

        #(schema,type) to set(subscribed channel)
        self._type_to_channel = {}

        #index-stringname to set(subscribed channel)
        self._index_to_channel = {}

        self._id_to_channel = {}

        self.longTransactionThreshold = 1.0
        self.logFrequency = 10.0

        self._transactions = 0
        self._keys_set = 0
        self._index_values_updated = 0
        self._subscriptions_written = 0

    def _removeOldDeadConnections(self):        
        connection_index = keymapping.index_key(core_schema.Connection, " exists", True)
        oldIds = self._kvstore.getSetMembers(keymapping.index_key(core_schema.Connection, " exists", True))

        if oldIds:
            self._kvstore.setSeveral(
                {keymapping.data_key(core_schema.Connection, identity, " exists"):None for identity in oldIds},
                {},
                {connection_index: set(oldIds)}
                )

    def checkForDeadConnections(self):
        with self._lock:
            for c in list(self._clientChannels):
                if time.time() - self._clientChannels[c].lastHeartbeat > getHeartbeatInterval() * 4:
                    logging.info(
                        "Connection %s has not heartbeat in a long time. Killing it.", 
                        self._clientChannels[c].connectionObject._identity
                        )

                    c.close()

    def dropConnection(self, channel):
        with self._lock:
            if channel not in self._clientChannels:
                logging.error('Tried to drop a nonexistant channel')
                return

            connectedChannel = self._clientChannels[channel]

            for schema_name, typename in connectedChannel.subscribedTypes:
                self._type_to_channel[schema_name,typename].discard(connectedChannel)

            for index_key in connectedChannel.subscribedIndexKeys:
                self._index_to_channel[index_key].discard(connectedChannel)
                if not self._index_to_channel[index_key]:
                    del self._index_to_channel[index_key]

            for identity in connectedChannel.subscribedIds:
                self._id_to_channel[identity].discard(connectedChannel)
                if not self._id_to_channel[identity]:
                    del self._id_to_channel[identity]

            co = connectedChannel.connectionObject

            logging.info("Server dropping connection for connectionObject._identity = %s", co._identity)

            del self._clientChannels[channel]

            self._dropConnectionEntry(co)

    def _createConnectionEntry(self):
        identity = sha_hash(str(uuid.uuid4())).hexdigest
        exists_key = keymapping.data_key(core_schema.Connection, identity, " exists")
        exists_index = keymapping.index_key(core_schema.Connection, " exists", True)

        self._handleNewTransaction(
            None,
            {exists_key: "true"},
            {exists_index: set([identity])},
            {},
            [],
            [],
            self._cur_transaction_num
            )

        return core_schema.Connection.fromIdentity(identity)

    def _dropConnectionEntry(self, entry):
        identity = entry._identity

        exists_key = keymapping.data_key(core_schema.Connection, identity, " exists")
        exists_index = keymapping.index_key(core_schema.Connection, " exists", True)

        self._handleNewTransaction(
            None,
            {exists_key: None},
            {},
            {exists_index: set([identity])},
            [],
            [],
            self._cur_transaction_num
            )

    def addConnection(self, channel):
        try:
            with self._lock:
                connectionObject = self._createConnectionEntry()

                connectedChannel = ConnectedChannel(self._cur_transaction_num, channel, connectionObject)

                self._clientChannels[channel] = connectedChannel

                channel.setClientToServerHandler(
                    lambda msg: self.onClientToServerMessage(connectedChannel, msg)
                    )

                connectedChannel.sendInitializationMessage()
        except:
            logging.error(
                "Failed during addConnection which should never happen:\n%s", 
                traceback.format_exc()
                )

    def _handleSubscription(self, connectedChannel, msg):
        schema_name = msg.schema
        
        #gather all the subscription information we need
        kvs = {}
        sets = {}

        definition = connectedChannel.definedSchemas.get(schema_name)

        assert definition is not None, "can't subscribe to a schema we don't know about!"

        t0 = time.time()

        if msg.typename is None:
            types_to_subscribe = list(definition)
            assert msg.fieldname_and_value is None, "Can't subscribe to a fieldname and value without a type"
        else:
            types_to_subscribe = [msg.typename]

        for typename in types_to_subscribe:
            assert typename in definition, "Can't subscribe to a type we didn't define in the schema: %s not in %s" % (typename, list(definition))

            typedef = definition[typename]

            if msg.fieldname_and_value is None:
                field, val = " exists", keymapping.index_value_to_hash(True)
            else:
                field, val = msg.fieldname_and_value

            if field == '_identity':
                identities = set([val])
            else:
                identities = set(self._kvstore.getSetMembers(keymapping.index_key_from_names_encoded(schema_name, typename, field, val)))

            for fieldname in typedef.fields:
                keys = [keymapping.data_key_from_names(schema_name, typename, identity, fieldname)
                                for identity in identities]

                vals = self._kvstore.getSeveral(keys)

                for i in range(len(keys)):
                    kvs[keys[i]] = vals[i]

            for fieldname in typedef.indices:
                index_group = keymapping.index_group(schema_name, typename, fieldname)
                index_vals = self._kvstore.getSetMembers(index_group)

                for iv in index_vals:
                    index_key = keymapping.index_group_and_hashval_to_index_key(index_group, iv)
                    sets[index_key] = self._kvstore.getSetMembers(index_key).intersection(identities)
                    if not sets[index_key]:
                        del sets[index_key]

            if msg.fieldname_and_value:
                #this is an index subscription
                for ident in identities:
                    self._id_to_channel.setdefault(ident, set()).add(connectedChannel)
                    connectedChannel.subscribedIds.add(ident)

                if msg.fieldname_and_value[0] != '_identity':
                    index_key = keymapping.index_key_from_names_encoded(msg.schema, msg.typename, msg.fieldname_and_value[0], msg.fieldname_and_value[1])

                    self._index_to_channel.setdefault(index_key, set()).add(connectedChannel)
                    connectedChannel.subscribedIndexKeys.add(index_key)
                else:
                    #an object's identity cannot change, so we don't need to track our subscription to it
                    pass
            else:
                #this is a type-subscription
                if (schema_name, typename) not in self._type_to_channel:
                    self._type_to_channel[schema_name, typename] = set()

                self._type_to_channel[schema_name, typename].add(connectedChannel)
                connectedChannel.subscribedTypes.add((schema_name, typename))

        connectedChannel.channel.write(
            ServerToClient.Subscription(
                schema=schema_name,
                typename=msg.typename,
                fieldname_and_value=msg.fieldname_and_value,
                values=kvs,
                sets=sets,
                tid=self._cur_transaction_num,
                identities=None if msg.fieldname_and_value is None else tuple(identities)
                )
            )

        if time.time() - t0 > self.longTransactionThreshold:
            logging.info(
                "Subscription for %s/%s/%s took %s seconds and produced %s values and %s sets with %s items.", 
                schema_name, msg.typename, msg.fieldname_and_value,
                time.time() - t0,
                len(kvs),
                len(sets),
                sum(len(s) for s in sets.values())
                )

    def onClientToServerMessage(self, connectedChannel, msg):
        assert isinstance(msg, ClientToServer)
        if msg.matches.Heartbeat:
            connectedChannel.heartbeat()
        elif msg.matches.Flush:
            with self._lock:
                connectedChannel.channel.write(ServerToClient.FlushResponse(guid=msg.guid))
        elif msg.matches.DefineSchema:
            connectedChannel.definedSchemas[msg.name] = msg.definition
        elif msg.matches.Subscribe:
            with self._lock:
                self._handleSubscription(connectedChannel, msg)
        elif msg.matches.NewTransaction:
            try:
                with self._lock:
                    isOK = self._handleNewTransaction(
                        connectedChannel,
                        {k: v for k,v in msg.writes.items()},
                        {k: set(a) for k,a in msg.set_adds.items() if a},
                        {k: set(a) for k,a in msg.set_removes.items() if a},
                        msg.key_versions,
                        msg.index_versions,
                        msg.as_of_version
                        )
            except:
                logging.error("Unknown error committing transaction: %s", traceback.format_exc())
                isOK = False

            connectedChannel.sendTransactionSuccess(msg.transaction_guid, isOK)

    def indexReverseLookupKvs(self, adds, removes):
        res = {}

        for indexKey, identities in removes.items():
            fieldname, valuehash = keymapping.split_index_key_to_fieldname_and_hash(indexKey)

            for ident in identities:
                res[keymapping.data_reverse_index_key(ident, fieldname)] = None

        for indexKey, identities in adds.items():
            fieldname, valuehash = keymapping.split_index_key_to_fieldname_and_hash(indexKey)

            for ident in identities:
                res[keymapping.data_reverse_index_key(ident, fieldname)] = valuehash

        return res

    def _broadcastSubscriptionIncrease(self, channel, indexKey, newIds):
        newIds = list(newIds)

        schema_name, typename, fieldname, fieldval = keymapping.split_index_key_full(indexKey)

        channel.channel.write(
            ServerToClient.SubscriptionIncrease(
                schema=schema_name,
                typename=typename,
                fieldname_and_value=(fieldname, fieldval),
                identities=newIds
                )
            )

    def _increaseBroadcastTransactionToInclude(self, channel, indexKey, newIds, key_value, set_adds, set_removes):
        #we need to include all the data for the objects in 'newIds' to the transaction
        #that we're broadcasting
        schema_name, typename, fieldname, fieldval = keymapping.split_index_key_full(indexKey)

        typedef = channel.definedSchemas.get(schema_name).get(typename)

        valsToGet = []
        for field_to_pull in typedef.fields:
            for ident in newIds:
                valsToGet.append(keymapping.data_key_from_names(schema_name,typename, ident, field_to_pull))

        results = self._kvstore.getSeveral(valsToGet)
        
        key_value.update({valsToGet[i]: results[i] for i in range(len(valsToGet))})

        reverseKeys = []
        for index_name in typedef.indices:
            for ident in newIds:
                reverseKeys.append(keymapping.data_reverse_index_key(ident, index_name))

        reverseVals = self._kvstore.getSeveral(reverseKeys)
        reverseKVMap = {reverseKeys[i]:reverseVals[i] for i in range(len(reverseKeys))}

        for index_name in typedef.indices:
            for ident in newIds:
                fieldval = reverseKVMap.get(keymapping.data_reverse_index_key(ident, index_name))

                if fieldval is not None:
                    ik = keymapping.index_key_from_names_encoded(schema_name, typename, index_name, fieldval)
                    set_adds.setdefault(ik, set()).add(ident)

    def _handleNewTransaction(self, 
                sourceChannel,
                key_value, 
                set_adds, 
                set_removes, 
                keys_to_check_versions, 
                indices_to_check_versions, 
                as_of_version
                ):
        """Commit a transaction. 

        key_value: a map
            db_key -> (json_representation, database_representation)
        that we want to commit. We cache the normal_representation for later.

        set_adds: a map:
            db_key -> set of identities added to an index
        set_removes: a map:
            db_key -> set of identities removed from an index
        """
        self._cur_transaction_num += 1
        transaction_id = self._cur_transaction_num
        assert transaction_id > as_of_version

        t0 = time.time()

        set_adds = {k:v for k,v in set_adds.items() if v}
        set_removes = {k:v for k,v in set_removes.items() if v}

        identities_mentioned = set()

        keysWritingTo = set()
        setsWritingTo = set()
        schemaTypePairsWriting = set()

        if sourceChannel:
            #check if we created any new objects to which we are not type-subscribed
            #and if so, ensure we are subscribed
            for add_index, added_identities in set_adds.items():
                schema_name, typename, fieldname, fieldval = keymapping.split_index_key_full(add_index)
                if fieldname == ' exists':
                    if (schema_name, typename) not in sourceChannel.subscribedTypes:
                        sourceChannel.subscribedIds.update(added_identities)
                        for new_id in added_identities:
                            self._id_to_channel.setdefault(new_id, set()).add(sourceChannel)
                        self._broadcastSubscriptionIncrease(sourceChannel, add_index, added_identities)

        for key in key_value:
            keysWritingTo.add(key)

            schema_name, typename, ident = keymapping.split_data_key(key)[:3]
            schemaTypePairsWriting.add((schema_name,typename))

            identities_mentioned.add(ident)

        for subset in [set_adds, set_removes]:
            for k in subset:
                if subset[k]:
                    schema_name, typename = keymapping.split_index_key(k)[:2]
                    
                    schemaTypePairsWriting.add((schema_name,typename))

                    setsWritingTo.add(k)

                    identities_mentioned.update(subset[k])

        #check all version numbers for transaction conflicts.
        for subset in [keys_to_check_versions, indices_to_check_versions]:
            for key in subset:
                last_tid = self._version_numbers.get(key, -1)
                if as_of_version < last_tid:
                    return False

        for key in keysWritingTo:
            self._version_numbers[key] = transaction_id

        for key in setsWritingTo:
            self._version_numbers[key] = transaction_id

        t1 = time.time()

        #set the json representation in the database
        target_kvs = {k: v for k,v in key_value.items()}
        target_kvs.update(self.indexReverseLookupKvs(set_adds, set_removes))

        new_sets, dropped_sets = self._kvstore.setSeveral(target_kvs, set_adds, set_removes)

        #each update the metadata index
        indexSetAdds = {}
        indexSetRemoves = {}
        for s in new_sets:
            index_key, index_val = keymapping.split_index_key(s)
            if index_key not in indexSetAdds:
                indexSetAdds[index_key] = set()
            indexSetAdds[index_key].add(index_val)

        for s in dropped_sets:
            index_key, index_val = keymapping.split_index_key(s)
            if index_key not in indexSetRemoves:
                indexSetRemoves[index_key] = set()
            indexSetRemoves[index_key].add(index_val)

        self._kvstore.setSeveral({}, indexSetAdds,indexSetRemoves)

        t2 = time.time()

        #check any index-level subscriptions that are going to increase as a result of this
        #transaction and add the backing data to the relevant transaction.
        for index_key, adds in list(set_adds.items()):
            if index_key in self._index_to_channel:
                idsToAddToTransaction = set()

                for channel in self._index_to_channel.get(index_key):
                    newIds = adds.difference(channel.subscribedIds)
                    for new_id in newIds:
                        self._id_to_channel.setdefault(new_id, set()).add(channel)
                        channel.subscribedIds.add(new_id)
    
                    self._broadcastSubscriptionIncrease(channel, index_key, newIds)

                    idsToAddToTransaction.update(newIds)

                if idsToAddToTransaction:
                    self._increaseBroadcastTransactionToInclude(
                        channel, #deliberately just using whatever random channel, under
                                 #the assumption they're all the same. it would be better
                                 #to explictly compute the union of the relevant set of defined fields,
                                 #as its possible one channel has more fields for a type than another
                                 #and we'd like to broadcast them all
                        index_key, idsToAddToTransaction, key_value, set_adds, set_removes)

        transaction_message = None
        channelsTriggered = set()

        for schema_type_pair in schemaTypePairsWriting:
            channelsTriggered.update(self._type_to_channel.get(schema_type_pair,()))

        for i in identities_mentioned:
            if i in self._id_to_channel:
                channelsTriggered.update(self._id_to_channel[i])

        for channel in channelsTriggered:
            if transaction_message is None:
                transaction_message = ServerToClient.Transaction(
                    writes={k:v for k,v in key_value.items()},
                    set_adds=set_adds,
                    set_removes=set_removes,
                    transaction_id=transaction_id
                    )

            channel.sendTransaction(transaction_message)
        
        if self.verbose or time.time() - t0 > self.longTransactionThreshold:
            logging.info("Transaction [%.2f/%.2f/%.2f] with %s writes, %s set ops: %s", 
                t1 - t0, t2 - t1, time.time() - t2,
                len(key_value), len(set_adds) + len(set_removes), sorted(key_value)[:3]
                )

        return True