'''
db.py

Copyright 2008 Andres Riancho

This file is part of w3af, w3af.sourceforge.net .

w3af is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 2 of the License.

w3af is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with w3af; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

'''
from __future__ import with_statement

import sqlite3
import sys

from multiprocessing.dummy import Queue, Process


class DBClient(object):
    """Simple w3af DB interface"""
    
    def __init__(self):
        '''Construct object.'''
        super(DBClient, self).__init__()
    
    def createTable(self, name, columns=(), primaryKeyColumns=[]):
        '''Create table in convenient way.'''
        #
        # Lets create the table
        #
        sql = 'CREATE TABLE ' + name + '('
        for columnData in columns:
            columnName, columnType = columnData
            sql += columnName + ' ' + columnType + ', '
        # Finally the PK
        sql += 'PRIMARY KEY (' + ','.join(primaryKeyColumns) + '))'

        self.execute(sql)
        self.commit()

    def createIndex(self, table, columns):
        '''
        Create index for speed and performance

        @parameter table: The table from which you want to create an index from
        @parameter columns: A list of column names.
        '''
        sql = 'CREATE INDEX %s_index ON %s( %s )' % (table, table, ','.join(columns) )
        
        self.execute(sql)
        self.commit()

    def close(self):
        '''Commit changes and close the connection to the underlying db.'''
        raise NotImplementedError

    def execute(self, sql, parameters=()):
        '''Execute SQL statement.'''
        raise NotImplementedError
    
    def executemany(self, sql, items):
        '''Execute many SQL statement.'''
        raise NotImplementedError

    def select(self, sql, parameters=()):
        '''Execute SELECT statement and return result as generators'''
        raise NotImplementedError

    def select_one(self, sql, parameters=()):
        '''Execute SELECT statement and return first result'''
        raise NotImplementedError
    
    
class DBClientSQLite(Process, DBClient):
    """
    Wrap sqlite connection in a way that allows concurrent requests from multiple
    threads.

    This is done by internally queueing the requests and processing them 
    sequentially in a separate thread (in the same order they arrived).

    """
    def __init__(self, filename, autocommit=False, journal_mode="OFF", 
                       cache_size=2000):
        
        super(DBClientSQLite, self).__init__()
        
        # Convert the filename to UTF-8, this is needed for windows, and special
        # characters, see:
        # http://www.sqlite.org/c3ref/open.html
        unicode_filename = filename.decode(sys.getfilesystemencoding())
        self.filename = unicode_filename.encode("utf-8")
        
        self.filename = filename
        self.autocommit = autocommit
        self.journal_mode = journal_mode
        self.cache_size = cache_size
        
        # Setting the size to 50 in order to avoid high memory consumption
        self.reqs = Queue(50)
        
        # Setting the thread to daemon mode so it dies with the rest of the
        # process
        self.daemon = True
        
        # This put/join is here in order to wait for the setup phase in the run
        # method to execute before we return from this method, also see 
        # get/task_done below.
        self.reqs.put(None)
        self.start()
        self.reqs.join()

    def run(self):
        '''
        This is the "main" method for this class, the one that
        consumes the commands which are sent to the Queue. The idea is to have
        the following architecture features:
            * Other parts of the framework which want to insert into the DB simply
              add an item to our input Queue and "forget about it" since it will
              be processed in another thread.
              
            * Only one thread accesses the sqlite3 object, which avoids many
            issues because of sqlite's non thread-safeness
            
        The only important thing to keep in mind is that before any SELECT
        query we need to join() the input Queue in order to make sure that all
        INSERTS were processed.
        
        Since this is a daemon thread, I don't need any break/poison-pill, simply
        perform a "while True" and the Queue.get() will make sure we don't have
        100% CPU usage in the loop. 
        '''
        
        #
        #    Setup phase
        #
        if self.autocommit:
            conn = sqlite3.connect(self.filename, isolation_level=None, 
                                   check_same_thread=True)
        else:
            conn = sqlite3.connect(self.filename, check_same_thread=True)
        conn.execute('PRAGMA journal_mode = %s' % self.journal_mode)
        conn.execute('PRAGMA cache_size = %s' % self.cache_size)
        conn.text_factory = str
        cursor = conn.cursor()
        cursor.execute('PRAGMA synchronous=OFF')
        self.reqs.get()
        self.reqs.task_done()
        #
        #    End setup phase
        #
        
        while True:
            req, arg, res = self.reqs.get()
            if req == '--close--':
                break
            elif req == '--commit--':
                conn.commit()
            else:
                try:
                    cursor.execute(req, arg)
                except Exception, e:
                    print e, req, arg
                else:
                    if res:
                        for rec in cursor:
                            res.put(rec)
                        res.put('--no more--')
                    if self.autocommit:
                        conn.commit()
        conn.close()

    def execute(self, sql, parameters=None, res=None):
        """
        `execute` calls are non-blocking: just queue up the request and
        return immediately.
        """
        self.reqs.put((sql, parameters or tuple(), res))

    def executemany(self, sql, items):
        for item in items:
            self.execute(sql, item)

    def select(self, sql, parameters=None):
        """
        Unlike sqlite's native select, this select doesn't handle iteration
        efficiently.

        The result of `select` starts filling up with values as soon as the
        request is dequeued, and although you can iterate over the result normally
        (`for res in self.select(): ...`), the entire result will be in memory.
        """
        res = Queue() # results of the select will appear as items in this queue
        self.execute(sql, parameters, res)
        while True:
            rec = res.get()
            if rec == '--no more--':
                break
            yield rec

    def select_one(self, sql, parameters=None):
        """Return only the first row of the SELECT, or None if there are no
        matching rows."""
        try:
            return iter(self.select(sql, parameters)).next()
        except StopIteration:
            return None

    def commit(self):
        self.execute('--commit--')

    def close(self):
        self.filename = None        
        self.execute('--close--')

    def getFileName(self):
        '''Return DB filename.'''
        return self.filename

# Use this client
DB = DBClientSQLite


class WhereHelper(object):
    '''Simple WHERE condition maker.'''
    conditions = {}
    _values = []

    def __init__(self, conditions = {}):
        '''Construct object.'''
        self.conditions = conditions

    def values(self):
        '''Return values for prep.statements.'''
        if not self._values:
            self.sql()
        return self._values

    def _makePair(self, field, value, oper='=',  conjunction='AND'):
        '''Auxiliary method.'''
        result = ' ' + conjunction + ' ' + field + ' ' + oper + ' ?'
        return (result, value)

    def sql(self, whereStr=True):
        '''
        @return: SQL string.
        
        >>> w = WhereHelper( [ ('field', '3', '=') ] )
        >>> w.sql()
        ' WHERE field = ?'

        >>> w = WhereHelper( [ ('field', '3', '='), ('foo', '4', '=') ] )
        >>> w.sql()
        ' WHERE field = ? AND foo = ?'
        >>>
        '''
        result = ''
        self._values = []

        for cond in self.conditions:
            if isinstance(cond[0], list):
                item, oper = cond
                tmpWhere = ''
                for tmpField in item:
                    tmpName, tmpValue, tmpOper = tmpField
                    sql, value = self._makePair(tmpName, tmpValue, tmpOper, oper)
                    self._values.append(value)
                    tmpWhere += sql
                if tmpWhere:
                    result += " AND (" + tmpWhere[len(oper)+1:] + ")"
            else:
                sql, value = self._makePair(cond[0], cond[1], cond[2])
                self._values.append(value)
                result += sql
        result = result[5:]

        if whereStr and result:
            result = ' WHERE ' + result

        return result

    def __str__(self):
        return self.sql() + ' | ' + str(self.values())


