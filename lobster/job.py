import logging
import os

class JobProvider:
    def __init__(self):
        pass

    def done(self):
        raise NotImplementedError

    def obtain(self):
        raise NotImplementedError

    def release(self, id, return_code, output, task):
        raise NotImplementedError

    def work_left(self):
        raise NotImplementedError

class SimpleJobProvider(JobProvider):
    def __init__(self, config):
        self.__workdir = config.get('workdir', os.getcwd())
        self.__stageoutdir = config.get('stageout location', os.getcwd())
        self.__cmd = config.get('cmd')
        self.__max = config.get('max')
        self.__inputs = config.get('inputs', [])
        self.__outputs = config.get('outputs', [])
        self.__done = 0
        self.__running = 0
        self.__id = 0

        if not os.path.exists(self.__stageoutdir):
            os.makedirs(self.__stageoutdir)

    def done(self):
        return self.__done == self.__max

    def obtain(self, num=1, bijective=False):
        if bijective:
            raise NotImplementedError

        tasks = []

        for i in range(num):
            if self.__id < self.__max:
                self.__running += 1
                self.__id += 1

                inputs = [(x, x) for x in self.__inputs]
                outputs = [(os.path.join(self.__stageoutdir, x.replace(x, '%s_%s' % (self.__id, x))), x) for x in self.__outputs]

                logging.info("creating {0}".format(self.__id))
                tasks.append((str(self.__id), self.__cmd, inputs, outputs))
            else:
                break

        return tasks

    def release(self, tasks):
        for task in tasks:
            self.__running -= 1
            if task.return_status == 0:
                self.__done += 1
            logging.info("job {0} returned with return code {1} [{2} jobs finished / {3} total ]".format(task.tag, task.return_status, self.__done, self.__max))

            if task.output:
                f = gzip.open(os.path.join(self.__stageoutdir, task.tag+'_job.log.gz'), 'wb')
                f.write(task.output)
                f.close()

    def work_left(self):
        return self.__max - self.__done
